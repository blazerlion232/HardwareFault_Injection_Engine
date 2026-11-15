"""
fi_engine.py - 16-bit toy CPU + hardware fault-injection engine
================================================================

Pure-Python, no third-party dependencies, no GUI code.  Everything here can be
driven headlessly (see test_engine.py) or from the Tkinter front end (fi_gui.py).

ARCHITECTURE (single-cycle: one instruction == one clock cycle)
---------------------------------------------------------------
* 8 general registers R0..R7 (16-bit), PC, SP (16-bit), flags Z N C V
* 512-word unified memory (16-bit words)
    0x000-0x0FF  code      (write-protected when the MPU is enabled)
    0x100-0x17F  data / results (compared against the golden run)
    0x180-0x1FF  stack     (SP starts at 0x200 and grows downward)
* Instruction word: [15:11] opcode | [10:8] rd | [7:5] rs | [7:0] imm8
* Optional protection mechanisms: parity on register file + memory,
  watchdog timer (needs KICK), memory protection unit (W^X).

FAULT MODELS
------------
Targets : REG, PC, SP, FLAGS, MEM, IR (fetch bus), ALU (result bus), CTRL (skip)
Models  : FLIP    - bit flip (SEU for storage, transient glitch for IR/ALU)
          STUCK0 / STUCK1 - stuck-at fault, active for `duration` cycles
                    (0 = permanent)
          SKIP    - instruction skip (clock / voltage glitch), CTRL only
Multi-bit upsets are modelled with `width` (adjacent bits starting at `bit`).

OUTCOME CLASSES (faulty run compared with a fault-free "golden" run)
--------------------------------------------------------------------
MASKED   finished normally, outputs + result memory identical
SDC      finished normally but outputs/results differ (silent data corruption)
CRASH    CPU exception (illegal opcode, bad address, stack fault, bad PC...)
HANG     did not halt within the cycle budget
DETECTED stopped by a protection mechanism (parity error / watchdog)
"""
from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# Machine constants
# --------------------------------------------------------------------------
MASK16 = 0xFFFF
NUM_REGS = 8
MEM_SIZE = 0x200
CODE_END = 0x100          # code region  [0x000, 0x100)
DATA_BASE = 0x100         # data region  [0x100, 0x180)
RESULT_END = 0x180        # compared against golden run
STACK_TOP = 0x200         # stack region [0x180, 0x200), grows down

ST_RUNNING = "RUNNING"
ST_HALTED = "HALTED"
ST_CRASH = "CRASH"
ST_DETECTED = "DETECTED"
ST_HANG = "HANG"

OUTCOMES = ["MASKED", "SDC", "CRASH", "HANG", "DETECTED"]

OPCODES: Dict[str, int] = {
    "NOP": 0, "HALT": 1, "LDI": 2, "LDH": 3, "MOV": 4, "ADD": 5, "SUB": 6,
    "AND": 7, "OR": 8, "XOR": 9, "SHL": 10, "SHR": 11, "ADDI": 12, "CMP": 13,
    "CMPI": 14, "LD": 15, "ST": 16, "JMP": 17, "JZ": 18, "JNZ": 19,
    "JLT": 20, "JGE": 21, "CALL": 22, "RET": 23, "PUSH": 24, "POP": 25,
    "OUT": 26, "MUL": 27, "KICK": 28, "NOT": 29,
}   # opcodes 30 and 31 are illegal
CODE_TO_NAME = {v: k for k, v in OPCODES.items()}

FMT: Dict[str, str] = {}
for _n in ("NOP", "HALT", "RET", "KICK"):
    FMT[_n] = "none"
for _n in ("LDI", "LDH", "ADDI", "CMPI", "SHL", "SHR"):
    FMT[_n] = "rd_imm"
for _n in ("MOV", "ADD", "SUB", "AND", "OR", "XOR", "CMP", "MUL"):
    FMT[_n] = "rd_rs"
for _n in ("PUSH", "POP", "OUT", "NOT"):
    FMT[_n] = "rd"
for _n in ("LD", "ST"):
    FMT[_n] = "mem"
for _n in ("JMP", "JZ", "JNZ", "JLT", "JGE", "CALL"):
    FMT[_n] = "addr"


def sext8(v: int) -> int:
    return v - 256 if v & 0x80 else v


def _par(v: int) -> int:
    return bin(v).count("1") & 1


# --------------------------------------------------------------------------
# Assembler / disassembler
# --------------------------------------------------------------------------
class AsmError(Exception):
    pass


@dataclass
class Program:
    name: str
    source: str
    words: List[int]
    data: Dict[int, int]
    labels: Dict[str, int]
    source_map: Dict[int, str]


def _reg(tok: str) -> int:
    m = re.fullmatch(r"[Rr]([0-7])", tok.strip())
    if not m:
        raise AsmError(f"expected a register R0-R7, got {tok.strip()!r}")
    return int(m.group(1))


def _mem_operand(tok: str) -> int:
    m = re.fullmatch(r"\[\s*([Rr][0-7])\s*\]", tok.strip())
    if not m:
        raise AsmError(f"expected a memory operand like [R1], got {tok.strip()!r}")
    return _reg(m.group(1))


def _eval(expr: str, symbols: Dict[str, int]) -> int:
    expr = expr.strip()
    if not expr:
        raise AsmError("missing value")
    terms = re.findall(r"([+-]?)\s*([A-Za-z_0-9]+)", expr)
    if "".join(s + t for s, t in terms) != re.sub(r"\s+", "", expr):
        raise AsmError(f"cannot parse expression {expr!r}")
    total = 0
    for sign, tok in terms:
        if tok.isdigit():
            v = int(tok, 10)
        elif re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+", tok):
            v = int(tok, 0)
        elif tok in symbols:
            v = symbols[tok]
        else:
            raise AsmError(f"unknown symbol {tok!r}")
        total += -v if sign == "-" else v
    return total


def _w(name: str, rd: int = 0, rs: int = 0, imm: int = 0) -> int:
    op = OPCODES[name]
    if FMT[name] in ("rd_rs", "mem"):
        return (op << 11) | (rd << 8) | (rs << 5)
    return (op << 11) | (rd << 8) | (imm & 0xFF)


def _need(ops: List[str], n: int, mn: str) -> None:
    if len(ops) != n:
        raise AsmError(f"{mn} expects {n} operand(s), got {len(ops)}")


def _encode(mn: str, ops: List[str], sym: Dict[str, int]) -> List[int]:
    if mn == "LDA":                      # pseudo-op: LDA rd, value16  (2 words)
        _need(ops, 2, mn)
        rd = _reg(ops[0])
        v = _eval(ops[1], sym)
        if not -32768 <= v <= 65535:
            raise AsmError(f"value {v} does not fit in 16 bits")
        v &= MASK16
        return [_w("LDI", rd, 0, v & 0xFF), _w("LDH", rd, 0, v >> 8)]
    fmt = FMT[mn]
    if fmt == "none":
        _need(ops, 0, mn)
        return [_w(mn)]
    if fmt == "rd":
        _need(ops, 1, mn)
        return [_w(mn, _reg(ops[0]))]
    if fmt == "rd_rs":
        _need(ops, 2, mn)
        return [_w(mn, _reg(ops[0]), _reg(ops[1]))]
    if fmt == "mem":
        _need(ops, 2, mn)
        return [_w(mn, _reg(ops[0]), _mem_operand(ops[1]))]
    if fmt == "rd_imm":
        _need(ops, 2, mn)
        rd = _reg(ops[0])
        v = _eval(ops[1], sym)
        lo, hi = {"ADDI": (-128, 127), "CMPI": (-128, 127),
                  "SHL": (0, 15), "SHR": (0, 15)}.get(mn, (0, 255))
        if not lo <= v <= hi:
            raise AsmError(f"{mn} immediate {v} out of range {lo}..{hi} "
                           f"(use LDA + a register for larger values)")
        return [_w(mn, rd, 0, v & 0xFF)]
    if fmt == "addr":
        _need(ops, 1, mn)
        v = _eval(ops[0], sym)
        if not 0 <= v < CODE_END:
            raise AsmError(f"branch target 0x{v:X} outside code region")
        return [_w(mn, 0, 0, v)]
    raise AsmError(f"internal: bad format for {mn}")


def assemble(text: str, name: str = "program") -> Program:
    """Two-pass assembler.  Syntax:

        label:  MNEMONIC op, op      ; comment
        .equ    NAME, value
        .data   addr, v0, v1, ...    ; initial data memory (0x100-0x17F)
        LDA     rd, value16          ; pseudo-op = LDI + LDH
    """
    symbols: Dict[str, int] = {}
    labels: Dict[str, int] = {}
    items: List[tuple] = []
    addr = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        try:
            line = raw.split(";", 1)[0].strip()
            while line:
                m = re.match(r"^([A-Za-z_]\w*)\s*:(.*)$", line)
                if not m:
                    break
                lab = m.group(1)
                if lab in symbols:
                    raise AsmError(f"duplicate symbol {lab!r}")
                if re.fullmatch(r"[Rr][0-7]", lab) or lab.upper() in OPCODES or lab.upper() == "LDA":
                    raise AsmError(f"{lab!r} is a reserved word")
                symbols[lab] = labels[lab] = addr
                line = m.group(2).strip()
            if not line:
                continue
            parts = line.split(None, 1)
            head = parts[0]
            ops = [o.strip() for o in parts[1].split(",")] if len(parts) > 1 else []
            if head.startswith("."):
                d = head.lower()
                if d == ".equ":
                    if len(ops) != 2:
                        raise AsmError(".equ needs NAME, value")
                    symbols[ops[0]] = _eval(ops[1], symbols)
                elif d == ".data":
                    items.append((lineno, "data", None, ops, 0, raw.strip()))
                else:
                    raise AsmError(f"unknown directive {head!r}")
                continue
            mn = head.upper()
            if mn not in OPCODES and mn != "LDA":
                raise AsmError(f"unknown instruction {head!r}")
            items.append((lineno, "ins", mn, ops, addr, raw.strip()))
            addr += 2 if mn == "LDA" else 1
        except AsmError as e:
            raise AsmError(f"line {lineno}: {e}") from None

    if addr > CODE_END:
        raise AsmError(f"program too large: {addr} words (max {CODE_END})")
    words = [0] * addr
    data: Dict[int, int] = {}
    source_map: Dict[int, str] = {}
    for lineno, kind, mn, ops, a, raw in items:
        try:
            if kind == "data":
                if len(ops) < 2:
                    raise AsmError(".data needs an address and at least one value")
                base = _eval(ops[0], symbols)
                for k, tok in enumerate(ops[1:]):
                    ad = base + k
                    if not DATA_BASE <= ad < RESULT_END:
                        raise AsmError(f".data address 0x{ad:03X} outside data region "
                                       f"0x{DATA_BASE:03X}-0x{RESULT_END - 1:03X}")
                    val = _eval(tok, symbols)
                    if not -32768 <= val <= 65535:
                        raise AsmError(f"value {val} does not fit in 16 bits")
                    data[ad] = val & MASK16
            else:
                for k, w in enumerate(_encode(mn, ops, symbols)):
                    words[a + k] = w
                    source_map[a + k] = raw
        except AsmError as e:
            raise AsmError(f"line {lineno}: {e}") from None
    return Program(name, text, words, data, labels, source_map)


def disasm(word: int) -> str:
    op = (word >> 11) & 0x1F
    rd = (word >> 8) & 7
    rs = (word >> 5) & 7
    imm = word & 0xFF
    name = CODE_TO_NAME.get(op)
    if name is None:
        return f"??? (illegal opcode {op})"
    fmt = FMT[name]
    if fmt == "none":
        return name
    if fmt == "rd":
        return f"{name} R{rd}"
    if fmt == "rd_rs":
        return f"{name} R{rd}, R{rs}"
    if fmt == "mem":
        return f"{name} R{rd}, [R{rs}]"
    if fmt == "addr":
        return f"{name} 0x{imm:02X}"
    if name in ("ADDI", "CMPI"):
        return f"{name} R{rd}, {sext8(imm)}"
    if name in ("SHL", "SHR"):
        return f"{name} R{rd}, {imm & 15}"
    return f"{name} R{rd}, 0x{imm:02X}"


# --------------------------------------------------------------------------
# Fault description
# --------------------------------------------------------------------------
TARGETS = ["REG", "PC", "SP", "FLAGS", "MEM", "IR", "ALU", "CTRL"]
TARGET_BITS = {"REG": 16, "PC": 16, "SP": 16, "FLAGS": 4, "MEM": 16,
               "IR": 16, "ALU": 16, "CTRL": 1}
STORAGE_TARGETS = ("REG", "PC", "SP", "FLAGS", "MEM")
MODELS = ["FLIP", "STUCK0", "STUCK1", "SKIP"]


@dataclass
class FaultSpec:
    target: str
    model: str
    cycle: int
    index: int = 0          # register number or memory address
    bit: int = 0
    width: int = 1          # adjacent bits affected (multi-bit upset)
    duration: int = 1       # cycles the fault stays active; 0 = permanent

    def mask(self) -> int:
        return (((1 << self.width) - 1) << self.bit) & ((1 << TARGET_BITS[self.target]) - 1)

    def validate(self) -> None:
        if self.target not in TARGETS:
            raise ValueError(f"unknown target {self.target!r}")
        if self.target == "CTRL":
            if self.model != "SKIP":
                raise ValueError("control-logic faults use the instruction-skip model")
        elif self.model not in ("FLIP", "STUCK0", "STUCK1"):
            raise ValueError(f"model {self.model!r} is not valid for target {self.target}")
        nbits = TARGET_BITS[self.target]
        if self.target != "CTRL":
            if not 0 <= self.bit < nbits:
                raise ValueError(f"bit must be 0..{nbits - 1} for {self.target}")
            if self.width < 1 or self.bit + self.width > nbits:
                raise ValueError(f"bit + width exceeds the {nbits}-bit width of {self.target}")
        if self.target == "REG" and not 0 <= self.index < NUM_REGS:
            raise ValueError("register index must be 0..7")
        if self.target == "MEM" and not 0 <= self.index < MEM_SIZE:
            raise ValueError(f"memory address must be 0..0x{MEM_SIZE - 1:X}")
        if self.cycle < 0:
            raise ValueError("cycle must be >= 0")
        if self.duration < 0:
            raise ValueError("duration must be >= 0 (0 = permanent)")

    def describe(self) -> str:
        dur = "permanent" if self.duration == 0 else f"{self.duration} cycle(s)"
        if self.target == "CTRL":
            return f"instruction skip @cycle {self.cycle} for {dur}"
        w = f"[{self.bit}]" if self.width == 1 else f"[{self.bit + self.width - 1}:{self.bit}]"
        where = {"REG": f"R{self.index}", "PC": "PC", "SP": "SP", "FLAGS": "FLAGS",
                 "MEM": f"MEM[0x{self.index:03X}]", "IR": "IR (fetch bus)",
                 "ALU": "ALU result"}[self.target]
        if self.model == "FLIP":
            if self.target in STORAGE_TARGETS:
                return f"{where}{w} bit-flip @cycle {self.cycle}"
            return f"{where}{w} bit-flip @cycle {self.cycle} for {dur}"
        kind = "stuck-at-0" if self.model == "STUCK0" else "stuck-at-1"
        return f"{where}{w} {kind} from cycle {self.cycle}, {dur}"


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------
@dataclass
class CPUConfig:
    parity: bool = False        # parity bit on every register and memory word
    watchdog: bool = False      # reset (= detection) if no KICK within wdt_limit
    wdt_limit: int = 64
    mpu: bool = True            # code write-protected, no execution from data


class Trap(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class RunResult:
    status: str
    detail: str
    cycles: int
    outputs: List[int]
    regs: List[int]
    pc: int
    sp: int
    flags: int
    mem: List[int]
    activated: bool
    fetched: Set[int]
    touched: Set[int]


class CPU:
    TRACE_LIMIT = 5000

    def __init__(self, program: Program, cfg: Optional[CPUConfig] = None,
                 fault: Optional[FaultSpec] = None, trace: bool = False,
                 record_states: bool = False):
        self.program = program
        self.cfg = cfg or CPUConfig()
        self.fault = fault
        self.regs = [0] * NUM_REGS
        self.reg_par = [0] * NUM_REGS
        self.mem = [0] * MEM_SIZE
        for i, w in enumerate(program.words):
            self.mem[i] = w
        for a, v in program.data.items():
            self.mem[a] = v & MASK16
        self.mem_par = [_par(v) for v in self.mem]
        self.pc = 0
        self.sp = STACK_TOP
        self.flags = 0          # bit0 Z, bit1 N, bit2 C, bit3 V
        self.cycle = 0
        self.wdt = 0
        self.status = ST_RUNNING
        self.detail = ""
        self.outputs: List[int] = []
        self.activated = False
        self.fetched: Set[int] = set()
        self.touched: Set[int] = set()
        self.trace_on = trace
        self.trace: List[tuple] = []     # (cycle, pc, word|None, text, note)
        self.record_states = record_states
        self.states: List[tuple] = []
        self._note: List[str] = []
        self._force_noted = False
        self.next_pc = 0

    # ---------------- helpers ----------------
    def snapshot(self) -> tuple:
        return (tuple(self.regs), self.pc, self.sp, self.flags)

    def result(self) -> RunResult:
        return RunResult(self.status, self.detail, self.cycle, list(self.outputs),
                         list(self.regs), self.pc, self.sp, self.flags, list(self.mem),
                         self.activated, set(self.fetched), set(self.touched))

    def _active(self, cyc: int) -> bool:
        f = self.fault
        if f is None or cyc < f.cycle:
            return False
        return f.duration == 0 or cyc < f.cycle + f.duration

    def _mutate(self, value: int) -> int:
        f = self.fault
        m = f.mask()
        if f.model == "FLIP":
            return value ^ m
        if f.model == "STUCK0":
            return value & ~m
        if f.model == "STUCK1":
            return value | m
        return value

    # ---------------- fault injection into storage ----------------
    def _storage_get(self) -> int:
        f = self.fault
        t = f.target
        if t == "REG":
            return self.regs[f.index]
        if t == "PC":
            return self.pc
        if t == "SP":
            return self.sp
        if t == "FLAGS":
            return self.flags
        return self.mem[f.index]

    def _storage_set(self, v: int) -> None:
        # raw write: deliberately does NOT update parity bits (that is the fault)
        f = self.fault
        t = f.target
        if t == "REG":
            self.regs[f.index] = v
        elif t == "PC":
            self.pc = v
        elif t == "SP":
            self.sp = v
        elif t == "FLAGS":
            self.flags = v
        else:
            self.mem[f.index] = v

    def _inject_storage(self, post: bool) -> None:
        f = self.fault
        if f is None or f.target not in STORAGE_TARGETS:
            return
        cyc = self.cycle - 1 if post else self.cycle
        if f.model == "FLIP":
            if post or self.cycle != f.cycle:
                return                      # one-shot SEU at start of its cycle
        elif not self._active(cyc):
            return
        old = self._storage_get()
        new = self._mutate(old) & ((1 << TARGET_BITS[f.target]) - 1)
        if new != old:
            self._storage_set(new)
            self.activated = True
            if f.model == "FLIP" or not self._force_noted:
                self._force_noted = True
                self._note.append(f"FAULT: {f.describe()}  (0x{old:04X} -> 0x{new:04X})")

    def _fault_fetch(self, word: int) -> int:
        f = self.fault
        if f is None or f.target not in ("IR", "CTRL") or not self._active(self.cycle):
            return word
        if f.target == "CTRL":
            if word != 0:
                self.activated = True
                self._note.append("FAULT: instruction skipped (replaced by NOP)")
            return 0
        new = self._mutate(word) & MASK16
        if new != word:
            self.activated = True
            self._note.append(f"FAULT: IR 0x{word:04X} -> 0x{new:04X}")
        return new

    # ---------------- guarded storage access ----------------
    def _rr(self, i: int) -> int:
        v = self.regs[i]
        if self.cfg.parity and _par(v) != self.reg_par[i]:
            raise Trap(ST_DETECTED, f"Register-file parity error on R{i} (value 0x{v:04X})")
        return v

    def _wr(self, i: int, v: int) -> None:
        v &= MASK16
        self.regs[i] = v
        self.reg_par[i] = _par(v)

    def _read_mem(self, addr: int, what: str) -> int:
        v = self.mem[addr]
        if self.cfg.parity and _par(v) != self.mem_par[addr]:
            raise Trap(ST_DETECTED, f"Memory parity error at 0x{addr:03X} during {what}")
        return v

    def _write_mem(self, addr: int, v: int) -> None:
        v &= MASK16
        self.mem[addr] = v
        self.mem_par[addr] = _par(v)

    def _load(self, addr: int) -> int:
        if not 0 <= addr < MEM_SIZE:
            raise Trap(ST_CRASH, f"Load from unmapped address 0x{addr:04X}")
        self.touched.add(addr)
        return self._read_mem(addr, "load")

    def _store(self, addr: int, v: int) -> None:
        lo = DATA_BASE if self.cfg.mpu else 0
        if not lo <= addr < MEM_SIZE:
            raise Trap(ST_CRASH, f"Store to protected/unmapped address 0x{addr:04X}")
        self.touched.add(addr)
        self._write_mem(addr, v)

    def _push(self, v: int) -> None:
        sp = (self.sp - 1) & MASK16
        if not DATA_BASE <= sp < STACK_TOP:
            raise Trap(ST_CRASH, f"Stack overflow / corrupted SP (SP=0x{sp:04X})")
        self.touched.add(sp)
        self._write_mem(sp, v)
        self.sp = sp

    def _pop(self) -> int:
        sp = self.sp
        if not DATA_BASE <= sp < STACK_TOP:
            raise Trap(ST_CRASH, f"Stack underflow / corrupted SP (SP=0x{sp:04X})")
        self.touched.add(sp)
        v = self._read_mem(sp, "stack pop")
        self.sp = (sp + 1) & MASK16
        return v

    # ---------------- ALU ----------------
    def _alu(self, op: str, a: int, b: int) -> int:
        M = MASK16
        c = v = 0
        if op == "ADD":
            full = a + b
            res = full & M
            c = full >> 16
            v = 1 if (a ^ res) & (b ^ res) & 0x8000 else 0
        elif op == "SUB":
            full = a - b
            res = full & M
            c = 1 if a < b else 0
            v = 1 if (a ^ b) & (a ^ res) & 0x8000 else 0
        elif op == "AND":
            res = a & b
        elif op == "OR":
            res = a | b
        elif op == "XOR":
            res = a ^ b
        elif op == "MUL":
            full = a * b
            res = full & M
            c = 1 if full > M else 0
        elif op == "SHL":
            sh = b & 15
            full = a << sh
            res = full & M
            c = (full >> 16) & 1
        elif op == "SHR":
            sh = b & 15
            res = a >> sh
            c = (a >> (sh - 1)) & 1 if sh else 0
        elif op == "NOT":
            res = ~a & M
        else:
            raise Trap(ST_CRASH, f"internal ALU op {op}")
        f = self.fault
        if f is not None and f.target == "ALU" and self._active(self.cycle):
            new = self._mutate(res) & M
            if new != res:
                self.activated = True
                self._note.append(f"FAULT: ALU result 0x{res:04X} -> 0x{new:04X}")
                res = new
        self.flags = (1 if res == 0 else 0) | ((res >> 15) << 1) | (c << 2) | (v << 3)
        return res

    # ---------------- execute ----------------
    def _execute(self, name: str, rd: int, rs: int, imm: int) -> None:
        R = self._rr
        if name == "NOP":
            pass
        elif name == "HALT":
            self.status = ST_HALTED
            self.detail = "HALT executed"
        elif name == "LDI":
            self._wr(rd, imm)
        elif name == "LDH":
            self._wr(rd, (R(rd) & 0xFF) | (imm << 8))
        elif name == "MOV":
            self._wr(rd, R(rs))
        elif name in ("ADD", "SUB", "AND", "OR", "XOR", "MUL"):
            a = R(rd)
            b = R(rs)
            self._wr(rd, self._alu(name, a, b))
        elif name == "CMP":
            a = R(rd)
            b = R(rs)
            self._alu("SUB", a, b)
        elif name in ("SHL", "SHR"):
            self._wr(rd, self._alu(name, R(rd), imm & 15))
        elif name == "ADDI":
            self._wr(rd, self._alu("ADD", R(rd), sext8(imm) & MASK16))
        elif name == "CMPI":
            self._alu("SUB", R(rd), sext8(imm) & MASK16)
        elif name == "NOT":
            self._wr(rd, self._alu("NOT", R(rd), 0))
        elif name == "LD":
            self._wr(rd, self._load(R(rs)))
        elif name == "ST":
            addr = R(rs)
            self._store(addr, R(rd))
        elif name == "JMP":
            self.next_pc = imm
        elif name == "JZ":
            if self.flags & 1:
                self.next_pc = imm
        elif name == "JNZ":
            if not self.flags & 1:
                self.next_pc = imm
        elif name == "JLT":
            if ((self.flags >> 1) & 1) != ((self.flags >> 3) & 1):
                self.next_pc = imm
        elif name == "JGE":
            if ((self.flags >> 1) & 1) == ((self.flags >> 3) & 1):
                self.next_pc = imm
        elif name == "CALL":
            self._push((self.pc + 1) & MASK16)
            self.next_pc = imm
        elif name == "RET":
            self.next_pc = self._pop()
        elif name == "PUSH":
            self._push(R(rd))
        elif name == "POP":
            self._wr(rd, self._pop())
        elif name == "OUT":
            self.outputs.append(R(rd))
        elif name == "KICK":
            self.wdt = -1           # becomes 0 after this cycle's increment

    def step(self) -> None:
        if self.status != ST_RUNNING:
            return
        self._note = []
        start = self.cycle
        pc = self.pc
        word: Optional[int] = None
        try:
            self._inject_storage(post=False)
            if self.cfg.watchdog and self.wdt > self.cfg.wdt_limit:
                raise Trap(ST_DETECTED, f"Watchdog expired: no KICK for {self.wdt} cycles")
            pc = self.pc
            limit = CODE_END if self.cfg.mpu else MEM_SIZE
            if not 0 <= pc < limit:
                raise Trap(ST_CRASH, f"Instruction fetch from invalid address 0x{pc:04X}")
            self.fetched.add(pc)
            word = self._read_mem(pc, "instruction fetch")
            word = self._fault_fetch(word)
            op = (word >> 11) & 0x1F
            name = CODE_TO_NAME.get(op)
            if name is None:
                raise Trap(ST_CRASH, f"Illegal instruction 0x{word:04X} at PC=0x{pc:04X}")
            self.next_pc = (pc + 1) & MASK16
            self._execute(name, (word >> 8) & 7, (word >> 5) & 7, word & 0xFF)
            self.pc = self.next_pc
            self.cycle += 1
            self.wdt += 1
            if self.status == ST_RUNNING:
                self._inject_storage(post=True)
        except Trap as t:
            self.status = t.kind
            self.detail = t.detail
            self._note.append(f"TRAP: {t.detail}")
        if self.trace_on:
            if len(self.trace) < self.TRACE_LIMIT:
                text = disasm(word) if word is not None else "----"
                self.trace.append((start, pc, word, text, "; ".join(self._note)))
            elif len(self.trace) == self.TRACE_LIMIT:
                self.trace.append((start, pc, None, "... trace truncated ...", ""))
        if self.record_states:
            self.states.append(self.snapshot())

    def run(self, max_cycles: int) -> None:
        while self.status == ST_RUNNING:
            if self.cycle >= max_cycles:
                self.status = ST_HANG
                self.detail = f"No HALT within {max_cycles} cycles"
                break
            self.step()


def run_program(program: Program, cfg: Optional[CPUConfig] = None,
                fault: Optional[FaultSpec] = None, max_cycles: int = 50000) -> RunResult:
    cpu = CPU(program, cfg, fault)
    cpu.run(max_cycles)
    return cpu.result()


# --------------------------------------------------------------------------
# Outcome classification and divergence analysis
# --------------------------------------------------------------------------
def _fmt_list(v: List[int], n: int = 12) -> str:
    s = ", ".join(f"0x{x:04X}" for x in v[:n])
    return f"[{s}{', ...' if len(v) > n else ''}]"


def classify(golden: RunResult, faulty: RunResult) -> Tuple[str, str]:
    if faulty.status == ST_DETECTED:
        return "DETECTED", faulty.detail
    if faulty.status == ST_CRASH:
        return "CRASH", faulty.detail
    if faulty.status == ST_HANG:
        return "HANG", faulty.detail
    out_same = faulty.outputs == golden.outputs
    g_res = golden.mem[DATA_BASE:RESULT_END]
    f_res = faulty.mem[DATA_BASE:RESULT_END]
    res_same = g_res == f_res
    if out_same and res_same:
        notes = []
        if not faulty.activated:
            notes.append("the fault never altered any state (e.g. the stuck-at value was "
                         "already present, or the skipped instruction was a NOP)")
        else:
            if faulty.cycles != golden.cycles:
                notes.append(f"timing changed ({golden.cycles} -> {faulty.cycles} cycles)")
            if (faulty.regs, faulty.pc, faulty.sp, faulty.flags) != \
               (golden.regs, golden.pc, golden.sp, golden.flags):
                notes.append("residual register/flag differences remain but outputs and "
                             "result memory are correct")
            if not notes:
                notes.append("the corrupted state was overwritten or never used")
        return "MASKED", "Outputs and results identical to golden run: " + "; ".join(notes)
    parts = []
    if not out_same:
        parts.append(f"output differs - golden {_fmt_list(golden.outputs)} vs faulty "
                     f"{_fmt_list(faulty.outputs)}")
    if not res_same:
        bad = [DATA_BASE + i for i, (a, b) in enumerate(zip(g_res, f_res)) if a != b]
        parts.append(f"{len(bad)} result word(s) differ (first at 0x{bad[0]:03X})")
    return "SDC", "Program finished normally but " + "; ".join(parts)


def first_divergence(gold_states: List[tuple], fault_states: List[tuple]
                     ) -> Optional[Tuple[int, List[str]]]:
    """Index of the first cycle after which architectural state differs."""
    for i, (a, b) in enumerate(zip(gold_states, fault_states)):
        if a != b:
            names = [f"R{k}" for k in range(NUM_REGS) if a[0][k] != b[0][k]]
            if a[1] != b[1]:
                names.append("PC")
            if a[2] != b[2]:
                names.append("SP")
            if a[3] != b[3]:
                names.append("FLAGS")
            return i, names
    return None


# --------------------------------------------------------------------------
# Fault campaigns (statistical fault injection)
# --------------------------------------------------------------------------
@dataclass
class CampaignConfig:
    trials: int = 500
    seed: int = 1234
    targets: List[str] = field(default_factory=lambda: list(TARGETS))
    models: List[str] = field(default_factory=lambda: ["FLIP"])
    stuck_duration: int = 0        # 0 = permanent
    transient_duration: int = 1    # for IR / ALU / CTRL faults


def random_fault(rng: random.Random, golden: RunResult,
                 targets: Optional[List[str]] = None,
                 models: Optional[List[str]] = None,
                 stuck_duration: int = 0, transient_duration: int = 1) -> FaultSpec:
    """Target classes are sampled uniformly so each can be compared fairly."""
    targets = targets or list(TARGETS)
    models = [m for m in (models or ["FLIP"]) if m in ("FLIP", "STUCK0", "STUCK1")] or ["FLIP"]
    t = rng.choice(targets)
    model = "SKIP" if t == "CTRL" else rng.choice(models)
    cycle = rng.randrange(max(1, golden.cycles))
    index = 0
    if t == "REG":
        index = rng.randrange(NUM_REGS)
    elif t == "MEM":
        addrs = sorted(golden.fetched | golden.touched) or [0]
        index = rng.choice(addrs)
    bit = rng.randrange(TARGET_BITS[t]) if t != "CTRL" else 0
    if model in ("STUCK0", "STUCK1"):
        duration = stuck_duration
    elif t in ("IR", "ALU", "CTRL"):
        duration = transient_duration
    else:
        duration = 1
    return FaultSpec(t, model, cycle, index, bit, 1, duration)


@dataclass
class CampaignRecord:
    index: int
    fault: FaultSpec
    outcome: str
    detail: str
    cycles: int
    activated: bool


class CampaignResult:
    def __init__(self, records: List[CampaignRecord], config: CampaignConfig,
                 golden_cycles: int, cancelled: bool = False):
        self.records = records
        self.config = config
        self.golden_cycles = golden_cycles
        self.cancelled = cancelled

    @staticmethod
    def _empty() -> Dict[str, int]:
        return {o: 0 for o in OUTCOMES}

    def counts(self) -> Dict[str, int]:
        c = self._empty()
        for r in self.records:
            c[r.outcome] += 1
        return c

    def by_target(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for r in self.records:
            out.setdefault(r.fault.target, self._empty())[r.outcome] += 1
        return {t: out[t] for t in TARGETS if t in out}

    @staticmethod
    def failure_rate(counts: Dict[str, int]) -> float:
        n = sum(counts.values())
        return 0.0 if n == 0 else (counts["SDC"] + counts["CRASH"] + counts["HANG"]) / n

    def to_csv(self, path: str) -> None:
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["trial", "target", "model", "index", "bit", "width", "cycle",
                        "duration", "activated", "outcome", "cycles", "detail"])
            for r in self.records:
                f = r.fault
                w.writerow([r.index, f.target, f.model, f.index, f.bit, f.width, f.cycle,
                            f.duration, int(r.activated), r.outcome, r.cycles, r.detail])


def hang_budget(golden: RunResult) -> int:
    return golden.cycles * 4 + 200


def run_campaign(program: Program, cfg: CPUConfig, golden: RunResult, cc: CampaignConfig,
                 progress: Optional[Callable[[int, int], None]] = None,
                 cancel: Optional[Callable[[], bool]] = None) -> CampaignResult:
    rng = random.Random(cc.seed)
    budget = hang_budget(golden)
    records: List[CampaignRecord] = []
    cancelled = False
    for i in range(cc.trials):
        if cancel is not None and cancel():
            cancelled = True
            break
        f = random_fault(rng, golden, cc.targets, cc.models,
                         cc.stuck_duration, cc.transient_duration)
        r = run_program(program, cfg, f, budget)
        outcome, detail = classify(golden, r)
        records.append(CampaignRecord(i, f, outcome, detail, r.cycles, r.activated))
        if progress is not None and (i % 5 == 0 or i == cc.trials - 1):
            progress(i + 1, cc.trials)
    return CampaignResult(records, cc, golden.cycles, cancelled)


# --------------------------------------------------------------------------
# Sample programs
# --------------------------------------------------------------------------
SAMPLE_PROGRAMS: Dict[str, str] = {
    "Fibonacci (10 terms)": """\
; Compute and store the first 10 Fibonacci numbers, also output them
        LDA  R5, 0x100      ; R5 = store pointer
        LDI  R0, 0          ; a
        LDI  R1, 1          ; b
        LDI  R4, 10         ; loop counter
loop:   ST   R0, [R5]
        OUT  R0
        ADDI R5, 1
        MOV  R2, R0
        ADD  R2, R1         ; next = a + b
        MOV  R0, R1
        MOV  R1, R2
        KICK                ; feed the watchdog
        ADDI R4, -1
        JNZ  loop
        HALT
""",
    "Sum + XOR checksum + subroutine": """\
; Sums 8 values, XOR-checksums them, then calls a subroutine (R0 = R0*3)
.equ ARRAY, 0x110
.data ARRAY, 12, 7, 3, 25, 9, 14, 6, 31
        LDA  R1, ARRAY
        LDI  R2, 8
        LDI  R0, 0
        LDI  R3, 0
sum:    LD   R4, [R1]
        ADD  R0, R4
        XOR  R3, R4
        ADDI R1, 1
        KICK
        ADDI R2, -1
        JNZ  sum
        LDA  R5, 0x100
        ST   R0, [R5]       ; result[0] = sum
        OUT  R0
        ADDI R5, 1
        ST   R3, [R5]       ; result[1] = xor checksum
        OUT  R3
        CALL triple         ; uses the stack (return address)
        ADDI R5, 1
        ST   R0, [R5]       ; result[2] = sum * 3
        OUT  R0
        HALT
triple: LDI  R6, 3
        MUL  R0, R6
        RET
""",
    "Bubble sort (8 values)": """\
; Bubble-sort 8 values in place, then output them
.equ ARRAY, 0x110
.data ARRAY, 42, 7, 19, 3, 88, 1, 56, 23
        LDI  R6, 7          ; outer passes
outer:  LDA  R1, ARRAY
        MOV  R7, R6         ; inner iterations
inner:  LD   R2, [R1]       ; cur
        MOV  R5, R1
        ADDI R5, 1
        LD   R3, [R5]       ; next
        CMP  R3, R2
        JGE  noswap         ; next >= cur -> already ordered
        ST   R3, [R1]
        ST   R2, [R5]
noswap: MOV  R1, R5
        KICK
        ADDI R7, -1
        JNZ  inner
        ADDI R6, -1
        JNZ  outer
        LDA  R1, ARRAY
        LDI  R7, 8
dump:   LD   R2, [R1]
        OUT  R2
        ADDI R1, 1
        KICK
        ADDI R7, -1
        JNZ  dump
        HALT
""",
    "PIN check (glitch target)": """\
; Classic fault-injection target: access is granted (0xAAAA) only when the
; entered PIN matches the stored PIN.  Entered PIN is WRONG here, so the
; golden output is 0x0000.  Can a glitch force 0xAAAA out?
.data 0x110, 0x1234, 0x1111
        LDA  R1, 0x110
        LD   R2, [R1]       ; stored PIN
        ADDI R1, 1
        LD   R3, [R1]       ; entered PIN
        CMP  R2, R3
        JNZ  denied
granted: LDA R0, 0xAAAA
        OUT  R0
        HALT
denied: LDI  R0, 0
        OUT  R0
        HALT
""",
}
