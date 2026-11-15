# Hardware Fault Injection Lab

Developed as a trial with assistance from Claude Code
A pure-Python simulator of a small 16-bit microprocessor, with a fault-injection engine and a Tkinter GUI. Pick a fault (what to break, which bit, when, and for how long), inject it, and watch how the CPU responds compared with a fault-free "golden" run. You can also run statistical campaigns of thousands of random injections to measure which components are most vulnerable and how much protection mechanisms help.

No third-party packages are required.

---

## Contents

- [Features](#features)
- [Project files](#project-files)
- [Requirements and installation](#requirements-and-installation)
- [Running it](#running-it)
- [Quick start (5 minutes)](#quick-start-5-minutes)
- [Using the GUI](#using-the-gui)
- [How outcomes are classified](#how-outcomes-are-classified)
- [The simulated CPU](#the-simulated-cpu)
- [Assembly language reference](#assembly-language-reference)
- [Fault models](#fault-models)
- [Protection mechanisms](#protection-mechanisms)
- [Fault campaigns](#fault-campaigns)
- [Experiments to try](#experiments-to-try)
- [Using the engine without the GUI](#using-the-engine-without-the-gui)
- [Running the tests](#running-the-tests)
- [Extending the project](#extending-the-project)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Simulated microprocessor:** 16-bit, single-cycle CPU with 8 registers, PC, SP, status flags, 512 words of memory, a stack, and 30 instructions.
- **Built-in assembler:** write or edit programs in assembly directly in the GUI (labels, `.equ`, `.data`, comments).
- **Fault injection into:**
  - Register file (R0-R7)
  - Program counter and stack pointer
  - Status flags (Z, N, C, V)
  - Any memory word (code, data, or stack)
  - Instruction register / fetch bus
  - ALU result bus
  - Control logic (instruction skip / glitch)
- **Fault models:** bit-flip (single-event upset or transient), stuck-at-0, stuck-at-1, instruction skip, with multi-bit upsets and configurable duration (including permanent).
- **Protection mechanisms:** parity on registers and memory, watchdog timer, memory protection (read-only code, no execute-from-data).
- **Response modelling:** every run is compared with a golden run and classified as Masked, Silent Data Corruption, Crash, Hang, or Detected.
- **Analysis views:** side-by-side register/flag comparison, memory diff, cycle-by-cycle execution trace, and "first point of divergence" reporting.
- **Single-step mode:** advance cycle by cycle and watch the fault propagate.
- **Campaign mode:** random injections with per-component statistics, a stacked-bar chart, CSV export, and double-click replay of any individual injection.
- **Reproducible:** campaigns are seeded, so the same seed gives identical results.

---

## Project files

| File | Purpose |
|---|---|
| `fi_engine.py` | The simulator: CPU, assembler, fault models, outcome classification, campaigns, sample programs. No GUI code, so it can run headless. |
| `fi_gui.py` | The Tkinter application. Calls the engine and displays results. |
| `test_engine.py` | Automated tests (`unittest`) for the engine. |
| `README.md` | This file. |

Keep all files in the same folder. `fi_gui.py` imports `fi_engine.py`.

---

## Requirements and installation

- **Python 3.8 or newer**
- **Tkinter** (included with most Python installers)

Check that Tkinter is available:

```
python3 -c "import tkinter; print('Tk', tkinter.TkVersion)"
```

If that fails:

| Platform | Fix |
|---|---|
| Debian / Ubuntu | `sudo apt install python3-tk` |
| Fedora | `sudo dnf install python3-tkinter` |
| macOS (Homebrew) | `brew install python-tk` |
| Windows / macOS | Install Python from [python.org](https://www.python.org/downloads/), which bundles Tk |

There is nothing else to install.

---

## Running it

```
python3 fi_gui.py        # macOS / Linux
python fi_gui.py         # Windows
```

**macOS note:** if you see a message about `TK_SILENCE_DEPRECATION`, it is only a warning that Apple's bundled Tk is deprecated. You can silence it with:

```
TK_SILENCE_DEPRECATION=1 python3 fi_gui.py
```

or add `export TK_SILENCE_DEPRECATION=1` to your `~/.zshrc`. Installing Python from python.org (which ships a modern Tk) removes the warning entirely.

---

## Quick start (5 minutes)

1. Start the app. The Fibonacci sample program is already loaded and its golden run is computed.
2. In **Fault configuration**, leave the target as *Register file*, set **Register #** to `0`, **Bit** to `3`, **Inject at cycle** to `6`, and model *Bit-flip*.
3. Click **Inject & run**.
4. The banner turns red: **SILENT DATA CORRUPTION**. Open the **State comparison** tab to see which registers differ and the outputs side by side, then the **Execution trace** tab to see the orange row where the fault hit.
5. Tick **Parity on register file + memory** in the left panel and inject the same fault again. The banner turns blue: **FAULT DETECTED BY PROTECTION MECHANISM**.

---

## Using the GUI

### Left panel

- **Program:** choose a sample or edit the assembly, then press **Assemble & load**. Assembly errors are shown in red with a line number. On success the golden (fault-free) run is computed automatically.
- **Code image:** the assembled program. The row highlighted blue is the faulty CPU's current PC; an orange row is a code word targeted by a memory fault.
- **Protection mechanisms:** parity, watchdog (with a cycle limit), and memory protection. Changing any of these recomputes the golden run and resets the session.

### Fault configuration

| Control | Meaning |
|---|---|
| **Target** | The hardware element to disturb. |
| **Fault model** | Bit-flip, stuck-at-0, stuck-at-1 (or instruction skip for control logic). |
| **Register # / Address** | Which register (0-7) or memory address (decimal or hex such as `0x110`). Disabled for targets that have no index. |
| **Bit** | Lowest affected bit. The maximum depends on the target (flags have 4 bits, most others 16). |
| **Width** | Number of adjacent bits affected, for multi-bit upsets. |
| **Inject at cycle** | The clock cycle when the fault begins. One instruction equals one cycle. |
| **Duration** | Number of cycles the fault stays active. `0` means permanent. Ignored for one-shot storage bit-flips. |

A hint line under the controls explains the exact behaviour of the current target/model combination.

### Buttons

| Button | Action |
|---|---|
| **Inject & run** | Run the whole program with the configured fault and show the result. |
| **Step 1 cycle** | Advance both the golden and faulty CPUs by one cycle (starts a session if none exists). |
| **Run to end** | Finish the current stepping session. |
| **Golden run only** | Run the program with no fault, to inspect normal behaviour. |
| **Random fault** | Fill in the form with a random fault (you still press Inject). |
| **Reset** | Clear the current session. |

### Result banner

After a run, the coloured banner shows the outcome class, the fault that was injected, an explanation, where the architectural state first diverged from the golden run, and whether the fault was *activated* (actually changed state).

### Tabs

- **State comparison:** golden and faulty values for R0-R7, PC, SP, flags, cycle count, and status. Differences are highlighted red with an XOR column showing exactly which bits differ. Output values are listed beneath.
- **Memory:** golden and faulty memory side by side (code, data/results, and stack regions). Red cells differ between runs; the orange cell is the faulted word.
- **Execution trace (faulty CPU):** one row per cycle. Orange = fault injected or activated; red = control flow differs from the golden run; purple = a trap (crash or detection). Traces are capped at 5,000 rows.
- **Fault campaign:** see [Fault campaigns](#fault-campaigns).

---

## How outcomes are classified

Each faulty run is compared with the fault-free golden run:

| Outcome | Meaning |
|---|---|
| **MASKED** | The program finished normally and its outputs and result memory are identical to the golden run. The fault had no visible effect (it may never have taken effect, or the corrupted state was overwritten or unused). |
| **SDC** (Silent Data Corruption) | The program finished normally but outputs or result memory differ. Usually the most dangerous outcome, because nothing signalled a problem. |
| **CRASH** | The CPU raised an exception: illegal instruction, fetch from an invalid address, load/store to an invalid or protected address, or stack overflow/underflow. |
| **HANG** | The program did not halt within the cycle budget (4x the golden run's length plus 200 cycles). |
| **DETECTED** | A protection mechanism stopped the run: a parity error or a watchdog expiry. |

Only the **data/results region** (`0x100-0x17F`) and the `OUT` values are compared. The stack region is ignored, because stale stack contents are not program results.

---

## The simulated CPU

A deliberately small, single-cycle design: each instruction takes exactly one clock cycle.

- **Registers:** `R0-R7` (16-bit general purpose), `PC`, `SP` (16-bit)
- **Flags:** `Z` (zero), `N` (negative), `C` (carry/borrow), `V` (signed overflow)
- **Word size:** 16 bits

### Memory map (512 words, unified)

| Range | Region | Notes |
|---|---|---|
| `0x000-0x0FF` | Code | Read-only when memory protection is on. Program counter must stay here. |
| `0x100-0x17F` | Data / results | Initialised with `.data`. Compared against the golden run. |
| `0x180-0x1FF` | Stack | `SP` starts at `0x200` and grows downward. Not compared. |

### Instruction encoding

```
 15      11 10   8 7    5 4     0
+----------+------+------+-------+
|  opcode  |  rd  |  rs  | (unused for register forms)
+----------+------+------+-------+
|  opcode  |  rd  |      imm8    |   (immediate forms)
+----------+------+--------------+
```

Unused bits are "don't care", so faults there are naturally masked. Opcodes 30 and 31 are illegal, so a fault that creates one causes an illegal-instruction crash.

---

## Assembly language reference

### Syntax

```
label:   MNEMONIC  operand, operand    ; comment
.equ     NAME, value
.data    address, value, value, ...
```

- Mnemonics and register names are case-insensitive. Labels are case-sensitive.
- Numbers: decimal (`10`), hex (`0x1F`), binary (`0b101`), negative (`-5`).
- Simple expressions with `+` and `-` are allowed: `ARRAY+1`.
- `.data` addresses must be in `0x100-0x17F`.
- Registers cannot be used as label names.

### Instructions

| Mnemonic | Operands | Description | Sets flags |
|---|---|---|---|
| `NOP` | | Do nothing | |
| `HALT` | | Stop execution normally | |
| `LDI` | `rd, imm` | `rd = imm` (0 to 255) | |
| `LDH` | `rd, imm` | Replace the high byte of `rd` with `imm` (0 to 255) | |
| `MOV` | `rd, rs` | `rd = rs` | |
| `ADD` | `rd, rs` | `rd = rd + rs` | Z N C V |
| `SUB` | `rd, rs` | `rd = rd - rs` | Z N C V |
| `AND` | `rd, rs` | `rd = rd & rs` | Z N |
| `OR` | `rd, rs` | `rd = rd \| rs` | Z N |
| `XOR` | `rd, rs` | `rd = rd ^ rs` | Z N |
| `MUL` | `rd, rs` | `rd = rd * rs` (low 16 bits) | Z N C |
| `NOT` | `rd` | `rd = ~rd` | Z N |
| `SHL` | `rd, imm` | Shift left by `imm` (0 to 15) | Z N C |
| `SHR` | `rd, imm` | Logical shift right by `imm` (0 to 15) | Z N C |
| `ADDI` | `rd, imm` | `rd = rd + imm` (-128 to 127) | Z N C V |
| `CMP` | `rd, rs` | Compute `rd - rs`, set flags only | Z N C V |
| `CMPI` | `rd, imm` | Compute `rd - imm` (-128 to 127), set flags only | Z N C V |
| `LD` | `rd, [rs]` | `rd = memory[rs]` | |
| `ST` | `rd, [rs]` | `memory[rs] = rd` | |
| `JMP` | `addr` | Jump | |
| `JZ` | `addr` | Jump if zero (Z = 1) | |
| `JNZ` | `addr` | Jump if not zero (Z = 0) | |
| `JLT` | `addr` | Jump if signed less-than (N != V) | |
| `JGE` | `addr` | Jump if signed greater-or-equal (N == V) | |
| `CALL` | `addr` | Push return address, jump | |
| `RET` | | Pop return address into PC | |
| `PUSH` | `rd` | Push `rd` onto the stack | |
| `POP` | `rd` | Pop the stack into `rd` | |
| `OUT` | `rd` | Append `rd` to the program's output list | |
| `KICK` | | Reset the watchdog timer | |

Branch targets are absolute code addresses (labels are the normal way to write them).

### Pseudo-instruction

| Mnemonic | Operands | Description |
|---|---|---|
| `LDA` | `rd, value16` | Load a full 16-bit value or address. Expands to `LDI` + `LDH` (always 2 words). |

Because `LDI` holds only 8 bits, use `LDA` to load data addresses such as `0x110`.

### Example program

```asm
.equ ARRAY, 0x110
.data ARRAY, 12, 7, 3, 25

        LDA  R1, ARRAY      ; pointer
        LDI  R2, 4          ; count
        LDI  R0, 0          ; sum
loop:   LD   R4, [R1]
        ADD  R0, R4
        ADDI R1, 1
        KICK                ; feed the watchdog
        ADDI R2, -1
        JNZ  loop
        OUT  R0
        HALT
```

**Tip:** if you plan to enable the watchdog, include `KICK` inside every loop at least once every *limit* cycles (default 64), otherwise even the fault-free run will trip the watchdog and the app will refuse to inject.

---

## Fault models

| Target | Where it acts | Models available |
|---|---|---|
| **Register file** | A bit in one of R0-R7 | Flip, stuck-at-0, stuck-at-1 |
| **Program counter** | A bit in the PC | Flip, stuck-at-0, stuck-at-1 |
| **Stack pointer** | A bit in SP | Flip, stuck-at-0, stuck-at-1 |
| **Status flags** | Z, N, C, V (bits 0-3) | Flip, stuck-at-0, stuck-at-1 |
| **Memory word** | A bit in any memory word (code, data, stack) | Flip, stuck-at-0, stuck-at-1 |
| **Instruction register** | The instruction word seen by the decoder (memory itself is untouched) | Flip, stuck-at-0, stuck-at-1 |
| **ALU result bus** | The ALU result before write-back and flag generation | Flip, stuck-at-0, stuck-at-1 |
| **Control logic** | Whole instruction | Instruction skip (replaced by NOP) |

### Timing semantics

- **Storage bit-flip (register, PC, SP, flags, memory):** a one-shot single-event upset applied once at the start of the chosen cycle. Duration is ignored.
- **Storage stuck-at:** the bit is forced to 0 or 1 at the start and the end of every active cycle, from the injection cycle for *duration* cycles (`0` = permanent hard fault).
- **IR, ALU, and control faults:** active for every cycle in `[cycle, cycle + duration)`.
- **Multi-bit upsets:** `Width` > 1 affects adjacent bits starting at `Bit`.

### A note on parity

Faults write storage directly without updating the parity bit. That is what makes parity detection possible, and it also means parity only catches errors that flip an *odd* number of bits. Faults on the instruction register or ALU are not stored values, so parity cannot see them.

---

## Protection mechanisms

All are optional and set in the left panel:

| Mechanism | What it does |
|---|---|
| **Parity** | Every register and memory word has a parity bit, updated on legitimate writes and checked on every read (including instruction fetch). A mismatch stops the run as **DETECTED**. |
| **Watchdog** | A counter increments every cycle and is reset by `KICK`. If it exceeds the limit the run stops as **DETECTED**. Catches hangs and runaway control flow. |
| **Memory protection (MPU)** | Code is read-only and the PC must stay in the code region. Writes to code and execution from data cause a **CRASH**. Turning it off allows self-modifying code and execution from data memory. |

Combine them in the campaign tab to see, for example, how parity shifts silent corruptions into detections.

---

## Fault campaigns

The **Fault campaign** tab runs many random injections and summarises them.

**Settings**

- **Injections:** number of random faults.
- **Seed:** the same seed reproduces identical results.
- **Stuck-at duration:** `0` means permanent.
- **IR/ALU/skip duration:** how many cycles transient faults on those targets last.
- **Targets / Models:** choose which components and fault models to sample.

**How faults are sampled**

- Each trial picks a target class uniformly from those you ticked, so every component gets a comparable number of samples. This helps compare components fairly.
- The injection cycle is uniform over the golden run's length.
- Register faults pick a random register. Memory faults pick only from addresses the golden run actually fetched or touched, so results are not diluted by unused memory.
- Bit positions are uniform within the target's width.

**Output**

- A stacked bar chart per component showing the proportion of each outcome.
- A table of counts and a **failure rate** per component: `(SDC + crash + hang) / injections`.
- A list of every injection. **Double-click a row** to load that fault into the single-injection panel and replay it with full visibility.
- **Export CSV...** saves every injection with its parameters, outcome, and detail text.

Campaigns run on a background thread so the window stays responsive. Use **Cancel** to stop early; results gathered so far are kept.

---

## Experiments to try

1. **Glitching a PIN check.** Load *PIN check (glitch target)*. The entered PIN is wrong, so the golden output is `0x0000` (denied). Set target *Control logic*, cycle `6` (the `JNZ` that guards access), and inject. The branch is skipped and the program outputs `0xAAAA` (granted): a classic instruction-skip attack.
2. **PC corruption.** On Fibonacci, flip PC bit 12 at cycle 10. The CPU fetches outside the code region and crashes.
3. **Parity on and off.** Inject the same register flip with parity off (silent corruption) and on (detected).
4. **Watchdog.** Use a stuck-at-1 on the loop counter (R4, bit 15, permanent, cycle 0) with the watchdog enabled and disabled. Compare *Hang* with *Detected*.
5. **Masked faults.** Flip a bit in an unused register (R7). Check the trace: the fault row appears but the program is unaffected.
6. **Stuck-at that never activates.** Stuck-at-0 on a bit that is already 0 reports *fault activated: no*.
7. **Protection comparison.** Run a 1,000-injection campaign on *Bubble sort* with no protection, then with parity, then with parity + watchdog. Compare the Detected and SDC columns per component.
8. **Multi-bit upsets.** Set *Width* to 2 or 3 on a register: an even number of flipped bits slips past parity.
9. **Custom program.** Write your own routine in the editor and see which components are most vulnerable in a campaign.

---

## Using the engine without the GUI

`fi_engine.py` has no GUI dependency:

```python
import fi_engine as E

prog = E.assemble(E.SAMPLE_PROGRAMS["Fibonacci (10 terms)"])
cfg = E.CPUConfig(parity=True)

golden = E.run_program(prog, cfg)
print(golden.status, golden.cycles, golden.outputs)

fault = E.FaultSpec(target="REG", model="FLIP", cycle=6, index=0, bit=3)
fault.validate()
faulty = E.run_program(prog, cfg, fault, max_cycles=E.hang_budget(golden))

outcome, detail = E.classify(golden, faulty)
print(outcome, "-", detail)

# A campaign
cc = E.CampaignConfig(trials=1000, seed=42, models=["FLIP", "STUCK0", "STUCK1"])
result = E.run_campaign(prog, cfg, golden, cc)
print(result.counts())
for target, counts in result.by_target().items():
    print(target, counts, f"{100 * E.CampaignResult.failure_rate(counts):.1f}% failure")
result.to_csv("campaign.csv")
```

### Key engine API

| Name | Purpose |
|---|---|
| `assemble(text)` | Assemble source into a `Program` (raises `AsmError`). |
| `CPUConfig(parity, watchdog, wdt_limit, mpu)` | Protection settings. |
| `CPU(program, cfg, fault, trace, record_states)` | A steppable CPU: `step()`, `run(max_cycles)`, `result()`. |
| `run_program(program, cfg, fault, max_cycles)` | Run to completion and return a `RunResult`. |
| `FaultSpec(target, model, cycle, index, bit, width, duration)` | Describe a fault. Call `validate()` before use. |
| `classify(golden, faulty)` | Returns `(outcome, explanation)`. |
| `first_divergence(gold_states, fault_states)` | First cycle where state differs. |
| `random_fault(rng, golden, ...)` | Sample one random fault. |
| `run_campaign(program, cfg, golden, config, progress, cancel)` | Run a campaign. |

---

## Running the tests

```
python3 test_engine.py
```

The suite (19 tests) checks the golden outputs of every sample program, assembler error handling, disassembly round-trips, each fault model, parity and watchdog detection, MPU behaviour, input validation, and campaign reproducibility.

---

## Extending the project

- **New instruction:** add it to `OPCODES` and `FMT` in `fi_engine.py` (the assembler and disassembler then pick it up), then add its behaviour to `CPU._execute`. Opcodes 30 and 31 are free; using them reduces illegal-opcode crashes.
- **New protection (e.g. ECC):** add a flag to `CPUConfig`, then implement checking and correction in the guarded accessors `_rr`, `_wr`, `_read_mem`, `_write_mem`.
- **New fault target or model:** add it to `TARGETS`, `TARGET_BITS`, and `FaultSpec.validate/describe`, then handle it in `CPU._inject_storage` (stored state) or alongside `_fault_fetch` / `_alu` (signals). Add labels in `fi_gui.py`.
- **New sample program:** add an entry to `SAMPLE_PROGRAMS` and a golden-output test in `test_engine.py`.
- **Multi-cycle timing or a pipeline:** the cycle model lives in `CPU.step`, so this is where to start.

---

## Limitations

- The CPU is **single-cycle**: one instruction equals one clock cycle. There is no pipeline, cache, interrupt system, or multi-cycle timing, so faults in pipeline registers or caches cannot be modelled.
- Faults act at the **architectural** level (registers, memory, buses), not at gate or transistor level.
- Parity detects only odd-bit errors, and there is no error *correction*.
- The golden run must halt cleanly. Programs with infinite loops, or that fail the watchdog without `KICK`, cannot be used for injection.
- Programs must fit in 256 code words, with data in `0x100-0x17F`.
- Campaign statistics depend on the sampling scheme (uniform over component classes, and over used memory only), so failure rates compare components rather than predicting real-world error rates.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'tkinter'` | Install Tk (see [Requirements](#requirements-and-installation)). |
| Warning about `TK_SILENCE_DEPRECATION` (macOS) | Harmless. Set the variable as described above, or install Python from python.org. |
| "Assembly error: line N: ..." | Fix the reported line. Common causes: immediate out of range (use `LDA` for large values), unknown label, or wrong operand count. |
| "The fault-free run did not halt cleanly" | The golden run failed. With the watchdog on, add `KICK` inside loops or raise the limit. Check for infinite loops. |
| "Invalid fault" dialog | A field is out of range (for example register > 7, bit beyond the target's width, or an address above `0x1FF`). |
| Injection cycle beyond program length | Allowed, but the fault will never fire, so the run is masked with "fault activated: no". |
| Window looks blank or garbled on macOS | The system Tk 8.5 can render poorly. Use Python from python.org or `brew install python-tk`. |
