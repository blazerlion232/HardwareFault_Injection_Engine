#!/usr/bin/env python3
"""
fi_gui.py - Tkinter front end for the hardware fault-injection engine (fi_engine.py)

Run:  python fi_gui.py

Workflow
  1. Pick / edit an assembly program and press "Assemble & load"  (golden run is computed)
  2. Choose protections (parity, watchdog, MPU) in the left panel
  3. Pick a fault: target, bit, model, injection cycle, duration
  4. "Inject & run" (or step through cycle by cycle) and compare with the golden run
  5. Campaign tab: thousands of random injections -> outcome statistics per component
"""

import queue
import random
import threading
import os
os.environ["TK_SILENCE_DEPRECATION"] = "1"
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import fi_engine as E

TARGET_LABELS = {
    "REG": "Register file (R0-R7)",
    "PC": "Program counter",
    "SP": "Stack pointer",
    "FLAGS": "Status flags (Z N C V)",
    "MEM": "Memory word (code / data / stack)",
    "IR": "Instruction register (fetch bus)",
    "ALU": "ALU result bus",
    "CTRL": "Control logic (instruction skip)",
}
MODEL_LABELS = {
    "FLIP": "Bit-flip (SEU / transient)",
    "STUCK0": "Stuck-at-0",
    "STUCK1": "Stuck-at-1",
    "SKIP": "Instruction skip (glitch)",
}
HINTS = {
    ("storage", "FLIP"): "One-shot single-event upset: the selected bit(s) are inverted once, at the "
                         "start of the chosen cycle. Parity bits are NOT updated, so parity "
                         "protection can notice it when the value is next read.",
    ("storage", "STUCK"): "Hard fault: the selected bit(s) are forced to the stuck value on every "
                          "cycle from the injection cycle for 'duration' cycles (0 = permanent).",
    ("IR", "ANY"): "Corrupts the instruction word seen by the decoder for the active cycles. "
                   "Memory (and its parity) stay intact - this models a fetch-bus / IR glitch.",
    ("ALU", "ANY"): "Corrupts the ALU result before write-back and before the Z/N flags are "
                    "computed, for the active cycles.",
    ("CTRL", "ANY"): "Instructions fetched during the active cycles are replaced by NOPs "
                     "(clock / voltage glitch style instruction skip).",
}
OUTCOME_COLORS = {"MASKED": "#2e7d32", "SDC": "#c62828", "CRASH": "#6a1b9a",
                  "HANG": "#ef6c00", "DETECTED": "#1565c0"}
OUTCOME_TITLES = {
    "MASKED": "MASKED - no visible effect",
    "SDC": "SILENT DATA CORRUPTION",
    "CRASH": "CPU CRASH / EXCEPTION",
    "HANG": "HANG / TIMEOUT",
    "DETECTED": "FAULT DETECTED BY PROTECTION MECHANISM",
}


def h16(v):
    return f"0x{v:04X} ({v})"


def flags_str(f):
    return f"Z={f & 1} N={(f >> 1) & 1} C={(f >> 2) & 1} V={(f >> 3) & 1}"


class FaultLab(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hardware Fault Injection Lab - 16-bit CPU")
        self.geometry("1500x940")
        self.minsize(1150, 740)
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass
        self.mono = tkfont.nametofont("TkFixedFont").copy()
        self.mono.configure(size=10)
        self.rev_target = {v: k for k, v in TARGET_LABELS.items()}
        self.rev_model = {v: k for k, v in MODEL_LABELS.items()}

        self.program = None
        self.golden = None
        self.golden_ok = False
        self.gold_cpu = None
        self.fault_cpu = None
        self.session_fault = None
        self.shown_trace = 0
        self.inject_iid = None
        self._marked = []
        self.camp_result = None
        self.camp_running = False
        self.cancel_flag = False
        self.camp_queue = None

        self._build_ui()
        first = next(iter(E.SAMPLE_PROGRAMS))
        self.var_sample.set(first)
        self.load_sample(first)

    # ================================================================== UI
    def _build_ui(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=6)
        right = ttk.Frame(paned, padding=6)
        paned.add(left, weight=1)
        paned.add(right, weight=4)
        self._build_left(left)
        self._build_right(right)
        self.on_target_change()

    # ---------------------------------------------------------------- left
    def _build_left(self, parent):
        pf = ttk.LabelFrame(parent, text="1 - Program (assembly)", padding=6)
        pf.pack(fill="x")
        top = ttk.Frame(pf)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="Sample").pack(side="left")
        self.var_sample = tk.StringVar()
        cb = ttk.Combobox(top, textvariable=self.var_sample, values=list(E.SAMPLE_PROGRAMS),
                          state="readonly", width=30)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.load_sample(self.var_sample.get()))
        ed = ttk.Frame(pf)
        ed.pack(fill="both")
        self.editor = tk.Text(ed, width=46, height=17, font=self.mono, undo=True, wrap="none")
        ys = ttk.Scrollbar(ed, orient="vertical", command=self.editor.yview)
        self.editor.configure(yscrollcommand=ys.set)
        self.editor.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        ttk.Button(pf, text="Assemble & load", command=self.assemble_and_load).pack(fill="x", pady=(4, 0))
        self.lbl_prog = ttk.Label(pf, text="", wraplength=400, justify="left")
        self.lbl_prog.pack(fill="x", pady=(4, 0))

        lf = ttk.LabelFrame(parent, text="Code image (blue = faulty-CPU PC, orange = faulted word)", padding=4)
        lf.pack(fill="both", expand=True, pady=6)
        self.listing = ttk.Treeview(lf, columns=("addr", "word", "instr"), show="headings", height=9)
        for c, t, w in (("addr", "Addr", 60), ("word", "Word", 70), ("instr", "Instruction", 200)):
            self.listing.heading(c, text=t)
            self.listing.column(c, width=w, anchor="w")
        ys2 = ttk.Scrollbar(lf, orient="vertical", command=self.listing.yview)
        self.listing.configure(yscrollcommand=ys2.set)
        self.listing.pack(side="left", fill="both", expand=True)
        ys2.pack(side="right", fill="y")
        self.listing.tag_configure("pc", background="#bbdefb")
        self.listing.tag_configure("fault", background="#ffe082")

        prot = ttk.LabelFrame(parent, text="Protection mechanisms (changing recomputes golden run)", padding=6)
        prot.pack(fill="x")
        self.var_parity = tk.BooleanVar(value=False)
        self.var_wdt = tk.BooleanVar(value=False)
        self.var_mpu = tk.BooleanVar(value=True)
        self.var_wdt_limit = tk.StringVar(value="64")
        ttk.Checkbutton(prot, text="Parity on register file + memory (detects odd-bit errors)",
                        variable=self.var_parity, command=self.on_cfg_change).pack(anchor="w")
        wrow = ttk.Frame(prot)
        wrow.pack(anchor="w")
        ttk.Checkbutton(wrow, text="Watchdog (program must KICK within", variable=self.var_wdt,
                        command=self.on_cfg_change).pack(side="left")
        sp = ttk.Spinbox(wrow, from_=4, to=5000, width=5, textvariable=self.var_wdt_limit,
                         command=self.on_cfg_change)
        sp.pack(side="left", padx=3)
        sp.bind("<Return>", lambda e: self.on_cfg_change())
        ttk.Label(wrow, text="cycles)").pack(side="left")
        ttk.Checkbutton(prot, text="Memory protection (code is read-only, no execute from data)",
                        variable=self.var_mpu, command=self.on_cfg_change).pack(anchor="w")

    # --------------------------------------------------------------- right
    def _build_right(self, parent):
        ff = ttk.LabelFrame(parent, text="2 - Fault configuration", padding=8)
        ff.pack(fill="x")
        self.var_target = tk.StringVar(value=TARGET_LABELS["REG"])
        self.var_model = tk.StringVar()
        self.var_index = tk.StringVar(value="0")
        self.var_bit = tk.StringVar(value="0")
        self.var_width = tk.StringVar(value="1")
        self.var_cycle = tk.StringVar(value="0")
        self.var_dur = tk.StringVar(value="1")

        ttk.Label(ff, text="Target").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.cb_target = ttk.Combobox(ff, textvariable=self.var_target, state="readonly", width=34,
                                      values=list(TARGET_LABELS.values()))
        self.cb_target.grid(row=0, column=1, columnspan=2, sticky="w")
        self.cb_target.bind("<<ComboboxSelected>>", self.on_target_change)
        ttk.Label(ff, text="Fault model").grid(row=0, column=3, sticky="e", padx=4)
        self.cb_model = ttk.Combobox(ff, textvariable=self.var_model, state="readonly", width=28)
        self.cb_model.grid(row=0, column=4, columnspan=2, sticky="w")
        self.cb_model.bind("<<ComboboxSelected>>", lambda e: self.update_hint())

        self.lbl_index = ttk.Label(ff, text="Register #")
        self.lbl_index.grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.ent_index = ttk.Entry(ff, textvariable=self.var_index, width=9)
        self.ent_index.grid(row=1, column=1, sticky="w")
        ttk.Label(ff, text="Bit").grid(row=1, column=2, sticky="e", padx=4)
        self.spin_bit = ttk.Spinbox(ff, from_=0, to=15, textvariable=self.var_bit, width=6)
        self.spin_bit.grid(row=1, column=3, sticky="w")
        ttk.Label(ff, text="Width (adjacent bits)").grid(row=1, column=4, sticky="e", padx=4)
        self.spin_width = ttk.Spinbox(ff, from_=1, to=8, textvariable=self.var_width, width=6)
        self.spin_width.grid(row=1, column=5, sticky="w")

        ttk.Label(ff, text="Inject at cycle").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.spin_cycle = ttk.Spinbox(ff, from_=0, to=100000, textvariable=self.var_cycle, width=9)
        self.spin_cycle.grid(row=2, column=1, sticky="w")
        ttk.Label(ff, text="Duration (0 = permanent)").grid(row=2, column=2, columnspan=2, sticky="e", padx=4)
        self.spin_dur = ttk.Spinbox(ff, from_=0, to=100000, textvariable=self.var_dur, width=6)
        self.spin_dur.grid(row=2, column=4, sticky="w")
        self.lbl_gold = ttk.Label(ff, text="", foreground="#37474f")
        self.lbl_gold.grid(row=2, column=5, sticky="w", padx=8)

        self.lbl_hint = ttk.Label(ff, text="", wraplength=950, foreground="#555", justify="left")
        self.lbl_hint.grid(row=3, column=0, columnspan=6, sticky="w", padx=4, pady=(6, 0))

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=6)
        for text, cmd in (("Inject & run", self.do_inject), ("Step 1 cycle", self.do_step),
                          ("Run to end", self.do_run_rest), ("Golden run only", self.do_golden),
                          ("Random fault", self.do_random), ("Reset", self.do_reset)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))

        self.banner = tk.Label(parent, text="READY", font=("TkDefaultFont", 14, "bold"), bg="#607d8b",
                               fg="white", anchor="w", padx=10, pady=7)
        self.banner.pack(fill="x")
        self.lbl_fault = ttk.Label(parent, text="", font=("TkDefaultFont", 10, "bold"))
        self.lbl_fault.pack(fill="x", pady=(4, 0))
        self.lbl_detail = ttk.Label(parent, text="Assemble a program, configure a fault and press 'Inject & run'.",
                                    wraplength=1050, justify="left")
        self.lbl_detail.pack(fill="x")
        self.lbl_div = ttk.Label(parent, text="", wraplength=1050, justify="left", foreground="#37474f")
        self.lbl_div.pack(fill="x", pady=(0, 4))

        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill="both", expand=True)
        self._build_state_tab()
        self._build_memory_tab()
        self._build_trace_tab()
        self._build_campaign_tab()

    def _build_state_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.tab_state = tab
        self.nb.add(tab, text="State comparison")
        self.state_tree = ttk.Treeview(tab, columns=("el", "g", "f", "x"), show="headings", height=14)
        for c, t, w in (("el", "Element", 90), ("g", "Golden", 250), ("f", "Faulty", 250), ("x", "Difference (XOR)", 160)):
            self.state_tree.heading(c, text=t)
            self.state_tree.column(c, width=w, anchor="w")
        for r in [f"R{i}" for i in range(8)] + ["PC", "SP", "FLAGS", "CYCLES", "STATUS"]:
            self.state_tree.insert("", "end", iid=r, values=(r, "", "", ""))
        self.state_tree.tag_configure("diff", background="#ffcdd2")
        self.state_tree.pack(fill="x")
        out = ttk.Frame(tab)
        out.pack(fill="x", pady=8)
        ttk.Label(out, text="Golden OUT:", width=12).grid(row=0, column=0, sticky="nw")
        ttk.Label(out, text="Faulty OUT:", width=12).grid(row=1, column=0, sticky="nw")
        self.lbl_out_g = ttk.Label(out, text="", wraplength=1000, font=self.mono, justify="left")
        self.lbl_out_f = ttk.Label(out, text="", wraplength=1000, font=self.mono, justify="left")
        self.lbl_out_g.grid(row=0, column=1, sticky="w")
        self.lbl_out_f.grid(row=1, column=1, sticky="w")

    def _build_memory_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Memory")
        ttk.Label(tab, text="Red = word differs from the other run; orange = faulted word.").pack(anchor="w")
        pw = ttk.PanedWindow(tab, orient="horizontal")
        pw.pack(fill="both", expand=True)
        self.mem_g = self._mk_memtext(pw, "Golden")
        self.mem_f = self._mk_memtext(pw, "Faulty")

    def _mk_memtext(self, pw, title):
        fr = ttk.LabelFrame(pw, text=title, padding=3)
        pw.add(fr, weight=1)
        t = tk.Text(fr, width=44, height=20, font=self.mono, wrap="none", state="disabled")
        ys = ttk.Scrollbar(fr, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=ys.set)
        t.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        t.tag_configure("diff", background="#ffcdd2")
        t.tag_configure("fault", background="#ffe082")
        t.tag_configure("sec", foreground="#1565c0", font=self.mono)
        t.tag_configure("hdr", foreground="#78909c")
        return t

    def _build_trace_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Execution trace (faulty CPU)")
        ttk.Label(tab, text="Orange = fault injected / activated; red = control flow differs from golden; "
                            "purple = trap (crash / detection).").pack(anchor="w")
        fr = ttk.Frame(tab)
        fr.pack(fill="both", expand=True)
        cols = ("cyc", "pc", "word", "instr", "note")
        self.trace_tree = ttk.Treeview(fr, columns=cols, show="headings")
        for c, t, w in (("cyc", "Cycle", 60), ("pc", "PC", 70), ("word", "Word", 70),
                        ("instr", "Instruction", 170), ("note", "Notes", 700)):
            self.trace_tree.heading(c, text=t)
            self.trace_tree.column(c, width=w, anchor="w")
        ys = ttk.Scrollbar(fr, orient="vertical", command=self.trace_tree.yview)
        self.trace_tree.configure(yscrollcommand=ys.set)
        self.trace_tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.trace_tree.tag_configure("inject", background="#ffe082")
        self.trace_tree.tag_configure("diverge", background="#ffcdd2")
        self.trace_tree.tag_configure("trap", background="#e1bee7")

    def _build_campaign_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.tab_camp = tab
        self.nb.add(tab, text="Fault campaign")
        ctl = ttk.LabelFrame(tab, text="Random fault-injection campaign (uses the protections selected on the left)", padding=6)
        ctl.pack(fill="x")
        self.var_trials = tk.StringVar(value="500")
        self.var_seed = tk.StringVar(value="1234")
        self.var_stuckdur = tk.StringVar(value="0")
        self.var_transdur = tk.StringVar(value="1")
        r0 = ttk.Frame(ctl)
        r0.pack(fill="x")
        ttk.Label(r0, text="Injections").pack(side="left")
        ttk.Spinbox(r0, from_=1, to=100000, width=7, textvariable=self.var_trials).pack(side="left", padx=(4, 12))
        ttk.Label(r0, text="Seed").pack(side="left")
        ttk.Entry(r0, width=8, textvariable=self.var_seed).pack(side="left", padx=(4, 12))
        ttk.Label(r0, text="Stuck-at duration (0 = permanent)").pack(side="left")
        ttk.Spinbox(r0, from_=0, to=100000, width=6, textvariable=self.var_stuckdur).pack(side="left", padx=(4, 12))
        ttk.Label(r0, text="IR/ALU/skip duration").pack(side="left")
        ttk.Spinbox(r0, from_=1, to=1000, width=5, textvariable=self.var_transdur).pack(side="left", padx=4)
        r1 = ttk.Frame(ctl)
        r1.pack(fill="x", pady=(6, 0))
        ttk.Label(r1, text="Targets:").pack(side="left")
        self.camp_targets = {}
        for t in E.TARGETS:
            v = tk.BooleanVar(value=True)
            self.camp_targets[t] = v
            ttk.Checkbutton(r1, text=t, variable=v).pack(side="left", padx=4)
        ttk.Label(r1, text="   Models:").pack(side="left")
        self.camp_models = {}
        for m, d in (("FLIP", True), ("STUCK0", False), ("STUCK1", False)):
            v = tk.BooleanVar(value=d)
            self.camp_models[m] = v
            ttk.Checkbutton(r1, text=m, variable=v).pack(side="left", padx=4)
        r2 = ttk.Frame(ctl)
        r2.pack(fill="x", pady=(6, 0))
        self.btn_camp = ttk.Button(r2, text="Run campaign", command=self.start_campaign)
        self.btn_camp.pack(side="left")
        self.btn_cancel = ttk.Button(r2, text="Cancel", command=self.cancel_campaign, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(r2, text="Export CSV...", command=self.export_csv).pack(side="left")
        self.progress = ttk.Progressbar(r2, length=250, mode="determinate")
        self.progress.pack(side="left", padx=12)
        self.lbl_camp = ttk.Label(r2, text="")
        self.lbl_camp.pack(side="left")

        mid = ttk.Frame(tab)
        mid.pack(fill="x", pady=6)
        self.chart = tk.Canvas(mid, height=120, bg="white", highlightthickness=1, highlightbackground="#cfd8dc")
        self.chart.pack(fill="x")
        self.chart.bind("<Configure>", lambda e: self.draw_chart())

        cols = ("t", "n", "MASKED", "SDC", "CRASH", "HANG", "DETECTED", "fail")
        self.sum_tree = ttk.Treeview(tab, columns=cols, show="headings", height=6)
        for c, t, w in (("t", "Component", 110), ("n", "Injections", 80), ("MASKED", "Masked", 80),
                        ("SDC", "SDC", 80), ("CRASH", "Crash", 80), ("HANG", "Hang", 80),
                        ("DETECTED", "Detected", 80), ("fail", "Failure rate*", 110)):
            self.sum_tree.heading(c, text=t)
            self.sum_tree.column(c, width=w, anchor="center")
        self.sum_tree.pack(fill="x")
        ttk.Label(tab, text="*Failure rate = (SDC + crash + hang) / injections.  Double-click a row below "
                            "to load that fault into the single-injection panel and replay it.").pack(anchor="w")
        fr = ttk.Frame(tab)
        fr.pack(fill="both", expand=True, pady=(4, 0))
        self.rec_tree = ttk.Treeview(fr, columns=("i", "f", "o", "d"), show="headings", height=8)
        for c, t, w in (("i", "#", 50), ("f", "Fault", 330), ("o", "Outcome", 90), ("d", "Detail", 650)):
            self.rec_tree.heading(c, text=t)
            self.rec_tree.column(c, width=w, anchor="w")
        ys = ttk.Scrollbar(fr, orient="vertical", command=self.rec_tree.yview)
        self.rec_tree.configure(yscrollcommand=ys.set)
        self.rec_tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        for o, c in OUTCOME_COLORS.items():
            self.rec_tree.tag_configure(o, foreground=c)
        self.rec_tree.bind("<Double-1>", self.on_record_double)

    # ============================================================ program
    def load_sample(self, name):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", E.SAMPLE_PROGRAMS[name])
        self.assemble_and_load(name)

    def get_cfg(self):
        try:
            lim = max(4, int(self.var_wdt_limit.get()))
        except ValueError:
            lim = 64
        return E.CPUConfig(parity=self.var_parity.get(), watchdog=self.var_wdt.get(),
                           wdt_limit=lim, mpu=self.var_mpu.get())

    def assemble_and_load(self, name="custom"):
        if not isinstance(name, str):
            name = "custom"
        try:
            prog = E.assemble(self.editor.get("1.0", "end"), name)
        except E.AsmError as e:
            self.lbl_prog.configure(text=f"Assembly error: {e}", foreground="#c62828")
            return
        self.program = prog
        self.listing.delete(*self.listing.get_children())
        for a, w in enumerate(prog.words):
            self.listing.insert("", "end", iid=f"a{a}", values=(f"0x{a:03X}", f"0x{w:04X}", E.disasm(w)))
        self._marked = []
        self.on_cfg_change()

    def on_cfg_change(self):
        if self.program is None:
            return
        self.do_reset(refresh=False)
        cpu = E.CPU(self.program, self.get_cfg())
        cpu.run(50000)
        self.golden = cpu.result()
        self.golden_ok = self.golden.status == E.ST_HALTED
        n = len(self.program.words)
        if self.golden_ok:
            self.lbl_prog.configure(
                text=f"Assembled {n} words. Golden run: {self.golden.cycles} cycles, "
                     f"{len(self.golden.outputs)} outputs.", foreground="#2e7d32")
            self.lbl_gold.configure(text=f"(golden run = {self.golden.cycles} cycles)")
            try:
                self.spin_cycle.configure(to=max(1, self.golden.cycles * 4))
            except tk.TclError:
                pass
        else:
            self.lbl_prog.configure(
                text=f"Assembled {n} words, but the fault-free run did not halt cleanly: "
                     f"{self.golden.status} - {self.golden.detail}. Fix the program (watchdog needs KICK) "
                     f"before injecting.", foreground="#c62828")
            self.lbl_gold.configure(text="(golden run failed)")
        self.refresh()

    # ====================================================== fault config UI
    def on_target_change(self, *_):
        t = self.rev_target.get(self.var_target.get(), "REG")
        bits = E.TARGET_BITS[t]
        if t == "REG":
            self.lbl_index.configure(text="Register #")
            self.ent_index.configure(state="normal")
            if not self.var_index.get().strip().isdigit() or int(self.var_index.get()) > 7:
                self.var_index.set("0")
        elif t == "MEM":
            self.lbl_index.configure(text="Address")
            self.ent_index.configure(state="normal")
            if self.var_index.get().strip() in ("", "-"):
                self.var_index.set("0x100")
        else:
            self.lbl_index.configure(text="Index")
            self.ent_index.configure(state="disabled")
        self.spin_bit.configure(to=max(0, bits - 1), state="disabled" if t == "CTRL" else "normal")
        self.spin_width.configure(state="disabled" if t == "CTRL" else "normal")
        try:
            if int(self.var_bit.get()) > bits - 1:
                self.var_bit.set(str(max(0, bits - 1)))
        except ValueError:
            self.var_bit.set("0")
        models = ["SKIP"] if t == "CTRL" else ["FLIP", "STUCK0", "STUCK1"]
        self.cb_model.configure(values=[MODEL_LABELS[m] for m in models])
        if self.var_model.get() not in [MODEL_LABELS[m] for m in models]:
            self.var_model.set(MODEL_LABELS[models[0]])
        self.update_hint()

    def update_hint(self):
        t = self.rev_target.get(self.var_target.get(), "REG")
        m = self.rev_model.get(self.var_model.get(), "FLIP")
        if t in E.STORAGE_TARGETS:
            key = ("storage", "FLIP" if m == "FLIP" else "STUCK")
        else:
            key = (t, "ANY")
        self.lbl_hint.configure(text=HINTS[key])

    def read_fault(self):
        t = self.rev_target[self.var_target.get()]
        m = self.rev_model[self.var_model.get()]
        try:
            idx = int(self.var_index.get().strip(), 0) if t in ("REG", "MEM") else 0
            bit = int(self.var_bit.get()) if t != "CTRL" else 0
            width = int(self.var_width.get()) if t != "CTRL" else 1
            cyc = int(self.var_cycle.get())
            dur = int(self.var_dur.get())
        except ValueError:
            raise ValueError("Index/bit/width/cycle/duration must be integers (address may use 0x hex).")
        f = E.FaultSpec(t, m, cyc, idx, bit, width, dur)
        f.validate()
        return f

    def set_fault_in_ui(self, f):
        self.var_target.set(TARGET_LABELS[f.target])
        self.on_target_change()
        self.var_model.set(MODEL_LABELS[f.model])
        if f.target == "REG":
            self.var_index.set(str(f.index))
        elif f.target == "MEM":
            self.var_index.set(f"0x{f.index:03X}")
        self.var_bit.set(str(f.bit))
        self.var_width.set(str(f.width))
        self.var_cycle.set(str(f.cycle))
        self.var_dur.set(str(f.duration))
        self.update_hint()

    # ================================================ simulation sessions
    def max_cycles(self):
        return E.hang_budget(self.golden)

    def new_session(self, fault):
        cfg = self.get_cfg()
        self.gold_cpu = E.CPU(self.program, cfg, None, trace=True, record_states=True)
        self.fault_cpu = E.CPU(self.program, cfg, fault, trace=True, record_states=True)
        self.session_fault = fault
        self.trace_tree.delete(*self.trace_tree.get_children())
        self.shown_trace = 0
        self.inject_iid = None

    def _step_both(self):
        self.gold_cpu.step()
        fc = self.fault_cpu
        fc.step()
        if fc.status == E.ST_RUNNING and fc.cycle >= self.max_cycles():
            fc.status = E.ST_HANG
            fc.detail = f"No HALT within {fc.cycle} cycles (golden run needs {self.golden.cycles})"

    def _finish_gold(self):
        while self.gold_cpu.status == E.ST_RUNNING and self.gold_cpu.cycle < 50000:
            self.gold_cpu.step()

    def _check_ready(self):
        if self.program is None or not self.golden_ok:
            messagebox.showwarning("Not ready", "Assemble a program whose fault-free run halts cleanly first.")
            return False
        return True

    def do_inject(self):
        if not self._check_ready():
            return
        try:
            fault = self.read_fault()
        except ValueError as e:
            messagebox.showerror("Invalid fault", str(e))
            return
        self.new_session(fault)
        self._run_session()

    def do_golden(self):
        if not self._check_ready():
            return
        self.new_session(None)
        self._run_session()

    def _run_session(self):
        while self.fault_cpu.status == E.ST_RUNNING:
            self._step_both()
        self._finish_gold()
        self.refresh()
        if self.inject_iid and self.trace_tree.exists(self.inject_iid):
            self.trace_tree.see(self.inject_iid)

    def do_step(self):
        if not self._check_ready():
            return
        if self.fault_cpu is None:
            try:
                fault = self.read_fault()
            except ValueError as e:
                messagebox.showerror("Invalid fault", str(e))
                return
            self.new_session(fault)
        if self.fault_cpu.status != E.ST_RUNNING:
            return
        self._step_both()
        if self.fault_cpu.status != E.ST_RUNNING:
            self._finish_gold()
        self.refresh()
        kids = self.trace_tree.get_children()
        if kids:
            self.trace_tree.see(kids[-1])

    def do_run_rest(self):
        if not self._check_ready():
            return
        if self.fault_cpu is None:
            self.do_inject()
            return
        self._run_session()

    def do_random(self):
        if not self._check_ready():
            return
        f = E.random_fault(random.Random(), self.golden)
        self.set_fault_in_ui(f)

    def do_reset(self, refresh=True):
        self.gold_cpu = None
        self.fault_cpu = None
        self.session_fault = None
        self.shown_trace = 0
        self.inject_iid = None
        self.trace_tree.delete(*self.trace_tree.get_children())
        if refresh:
            self.refresh()

    # ============================================================ refresh
    def refresh(self):
        self._refresh_state()
        self._refresh_memory()
        self._append_trace()
        self._mark_listing()
        self._update_banner()

    def _refresh_state(self):
        g, f = self.gold_cpu, self.fault_cpu
        rows = [f"R{i}" for i in range(8)] + ["PC", "SP", "FLAGS", "CYCLES", "STATUS"]
        if g is None or f is None:
            for r in rows:
                self.state_tree.item(r, values=(r, "", "", ""), tags=())
            self.lbl_out_g.configure(text="")
            self.lbl_out_f.configure(text="")
            return

        def vals(cpu):
            d = {f"R{i}": h16(cpu.regs[i]) for i in range(8)}
            d.update(PC=h16(cpu.pc), SP=h16(cpu.sp), FLAGS=flags_str(cpu.flags),
                     CYCLES=str(cpu.cycle), STATUS=cpu.status)
            return d

        gv, fv = vals(g), vals(f)
        raw = {f"R{i}": (g.regs[i], f.regs[i]) for i in range(8)}
        raw.update(PC=(g.pc, f.pc), SP=(g.sp, f.sp), FLAGS=(g.flags, f.flags))
        for r in rows:
            xor = ""
            differ = gv[r] != fv[r]
            if r in raw and raw[r][0] != raw[r][1]:
                xor = f"0x{raw[r][0] ^ raw[r][1]:04X}"
            self.state_tree.item(r, values=(r, gv[r], fv[r], xor), tags=("diff",) if differ else ())
        go = ", ".join(f"0x{v:04X}" for v in g.outputs) or "-"
        fo = ", ".join(f"0x{v:04X}" for v in f.outputs) or "-"
        self.lbl_out_g.configure(text=go)
        self.lbl_out_f.configure(text=fo, foreground="#c62828" if g.outputs != f.outputs and
                                 f.status != E.ST_RUNNING else "black")

    def _render_mem(self, widget, mem, other, fault_addr):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        sections = {0: "CODE  0x000-0x0FF", 0x100: "DATA / RESULTS  0x100-0x17F (compared)",
                    0x180: "STACK  0x180-0x1FF (grows down from 0x200)"}
        for row in range(0, E.MEM_SIZE, 8):
            if row in sections:
                widget.insert("end", f"-- {sections[row]}\n", "sec")
            widget.insert("end", f"{row:03X}: ", "hdr")
            for a in range(row, row + 8):
                tag = ()
                if a == fault_addr:
                    tag = ("fault",)
                elif other is not None and mem[a] != other[a]:
                    tag = ("diff",)
                widget.insert("end", f"{mem[a]:04X} ", tag)
            widget.insert("end", "\n")
        widget.configure(state="disabled")

    def _refresh_memory(self):
        g, f = self.gold_cpu, self.fault_cpu
        if g is None or f is None:
            for w in (self.mem_g, self.mem_f):
                w.configure(state="normal")
                w.delete("1.0", "end")
                w.configure(state="disabled")
            return
        fa = self.session_fault.index if (self.session_fault and self.session_fault.target == "MEM") else None
        self._render_mem(self.mem_g, g.mem, f.mem, fa)
        self._render_mem(self.mem_f, f.mem, g.mem, fa)

    def _append_trace(self):
        f, g = self.fault_cpu, self.gold_cpu
        if f is None or g is None:
            return
        ft, gt = f.trace, g.trace
        for i in range(self.shown_trace, len(ft)):
            cyc, pc, word, text, note = ft[i]
            gr = gt[i] if i < len(gt) else None
            tags = ()
            if "TRAP" in note:
                tags = ("trap",)
            elif "FAULT" in note:
                tags = ("inject",)
            elif word is not None and (gr is None or (gr[1], gr[2]) != (pc, word)):
                tags = ("diverge",)
            if "FAULT" in note and self.inject_iid is None:
                self.inject_iid = f"t{i}"
            self.trace_tree.insert("", "end", iid=f"t{i}", tags=tags, values=(
                cyc, f"0x{pc:04X}", "" if word is None else f"0x{word:04X}", text, note))
        self.shown_trace = len(ft)

    def _mark_listing(self):
        for iid in self._marked:
            if self.listing.exists(iid):
                self.listing.item(iid, tags=())
        self._marked = []
        if self.fault_cpu is None:
            return
        pc_iid = f"a{self.fault_cpu.pc}"
        marks = {}
        if self.listing.exists(pc_iid):
            marks[pc_iid] = ["pc"]
        sf = self.session_fault
        if sf and sf.target == "MEM":
            fi = f"a{sf.index}"
            if self.listing.exists(fi):
                marks.setdefault(fi, []).append("fault")
        for iid, tags in marks.items():
            self.listing.item(iid, tags=tuple(tags))
            self._marked.append(iid)
        if pc_iid in marks:
            self.listing.see(pc_iid)

    def _update_banner(self):
        fc = self.fault_cpu
        if fc is None:
            self._set_banner("READY", "#607d8b", "", "Configure a fault and press 'Inject & run' "
                             "(or step through cycle by cycle).", "")
            return
        sf = self.session_fault
        fdesc = "Fault: " + (sf.describe() if sf else "none (golden run)")
        if fc.status == E.ST_RUNNING:
            self._set_banner(f"RUNNING - cycle {fc.cycle}", "#455a64", fdesc,
                             "Press 'Step 1 cycle' or 'Run to end'.", self._divergence_text())
            return
        if sf is None:
            self._set_banner("GOLDEN RUN (no fault)", "#455a64", fdesc,
                             f"Status {fc.status} after {fc.cycle} cycles.", "")
            return
        outcome, detail = E.classify(self.golden, fc.result())
        extra = "Fault activated (changed architectural state): " + ("yes" if fc.activated else "no")
        div = self._divergence_text()
        self._set_banner(OUTCOME_TITLES[outcome], OUTCOME_COLORS[outcome], fdesc, detail,
                         (div + "   |   " if div else "") + extra)

    def _divergence_text(self):
        g, f = self.gold_cpu, self.fault_cpu
        if g is None or f is None:
            return ""
        d = E.first_divergence(g.states, f.states)
        if d is None:
            return "Architectural state has not diverged from the golden run."
        i, names = d
        return f"State first diverges from golden after cycle {i} ({', '.join(names)})."

    def _set_banner(self, title, color, fault_txt, detail, div):
        self.banner.configure(text=title, bg=color)
        self.lbl_fault.configure(text=fault_txt)
        self.lbl_detail.configure(text=detail)
        self.lbl_div.configure(text=div)

    # ============================================================ campaign
    def start_campaign(self):
        if self.camp_running or not self._check_ready():
            return
        targets = [t for t, v in self.camp_targets.items() if v.get()]
        models = [m for m, v in self.camp_models.items() if v.get()]
        if not targets:
            messagebox.showerror("Campaign", "Select at least one target.")
            return
        if not models and any(t != "CTRL" for t in targets):
            messagebox.showerror("Campaign", "Select at least one fault model.")
            return
        try:
            cc = E.CampaignConfig(trials=max(1, int(self.var_trials.get())), seed=int(self.var_seed.get()),
                                  targets=targets, models=models or ["FLIP"],
                                  stuck_duration=max(0, int(self.var_stuckdur.get())),
                                  transient_duration=max(1, int(self.var_transdur.get())))
        except ValueError:
            messagebox.showerror("Campaign", "Injections, seed and durations must be integers.")
            return
        cfg, prog, golden = self.get_cfg(), self.program, self.golden
        self.camp_running = True
        self.cancel_flag = False
        self.btn_camp.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress["value"] = 0
        self.lbl_camp.configure(text="starting...")
        q = queue.Queue()
        self.camp_queue = q

        def worker():
            try:
                res = E.run_campaign(prog, cfg, golden, cc, progress=lambda d, t: q.put(("p", d, t)),
                                     cancel=lambda: self.cancel_flag)
                q.put(("done", res))
            except Exception as ex:  # noqa: BLE001
                q.put(("err", ex))

        threading.Thread(target=worker, daemon=True).start()
        self.after(60, self._poll_campaign)

    def cancel_campaign(self):
        self.cancel_flag = True

    def _poll_campaign(self):
        try:
            while True:
                msg = self.camp_queue.get_nowait()
                if msg[0] == "p":
                    self.progress["value"] = 100.0 * msg[1] / max(1, msg[2])
                    self.lbl_camp.configure(text=f"{msg[1]}/{msg[2]} injections")
                elif msg[0] == "done":
                    self._campaign_done(msg[1])
                    return
                else:
                    self.camp_running = False
                    self.btn_camp.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    messagebox.showerror("Campaign failed", str(msg[1]))
                    return
        except queue.Empty:
            pass
        self.after(60, self._poll_campaign)

    def _campaign_done(self, res):
        self.camp_running = False
        self.camp_result = res
        self.btn_camp.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.progress["value"] = 100
        self.lbl_camp.configure(text=f"{len(res.records)} injections" + (" (cancelled)" if res.cancelled else ""))
        self.show_campaign_results()

    def _summary_rows(self):
        res = self.camp_result
        rows = []
        if res is None:
            return rows
        for t, c in res.by_target().items():
            rows.append((t, c))
        rows.append(("ALL", res.counts()))
        return rows

    def show_campaign_results(self):
        res = self.camp_result
        self.sum_tree.delete(*self.sum_tree.get_children())
        for label, c in self._summary_rows():
            n = sum(c.values())
            self.sum_tree.insert("", "end", values=(
                label, n, c["MASKED"], c["SDC"], c["CRASH"], c["HANG"], c["DETECTED"],
                f"{100 * E.CampaignResult.failure_rate(c):.1f}%"))
        self.rec_tree.delete(*self.rec_tree.get_children())
        for r in res.records:
            self.rec_tree.insert("", "end", iid=f"r{r.index}", tags=(r.outcome,),
                                 values=(r.index, r.fault.describe(), r.outcome, r.detail))
        self.draw_chart()

    def draw_chart(self):
        c = self.chart
        c.delete("all")
        rows = self._summary_rows()
        if not rows:
            c.configure(height=60)
            c.create_text(10, 30, anchor="w", fill="#78909c",
                          text="Run a campaign to see outcome distribution per component.")
            return
        W = max(c.winfo_width(), 400)
        left, right, barh, gap, top = 90, 70, 18, 8, 28
        c.configure(height=top + len(rows) * (barh + gap) + 6)
        x = left
        for o in E.OUTCOMES:
            c.create_rectangle(x, 8, x + 12, 20, fill=OUTCOME_COLORS[o], outline="")
            c.create_text(x + 16, 14, anchor="w", text=o.title(), font=("TkDefaultFont", 9))
            x += 95
        span = W - left - right
        for i, (label, counts) in enumerate(rows):
            y = top + i * (barh + gap)
            n = max(1, sum(counts.values()))
            c.create_text(left - 8, y + barh / 2, anchor="e", text=label,
                          font=("TkDefaultFont", 9, "bold" if label == "ALL" else "normal"))
            xx = left
            for o in E.OUTCOMES:
                w = span * counts[o] / n
                if w > 0:
                    c.create_rectangle(xx, y, xx + w, y + barh, fill=OUTCOME_COLORS[o], outline="white")
                    if w > 34:
                        c.create_text(xx + w / 2, y + barh / 2, fill="white", font=("TkDefaultFont", 8),
                                      text=f"{100 * counts[o] / n:.0f}%")
                xx += w
            c.create_text(W - right + 8, y + barh / 2, anchor="w", text=f"n={sum(counts.values())}",
                          font=("TkDefaultFont", 9))

    def on_record_double(self, _event):
        sel = self.rec_tree.selection()
        if not sel or self.camp_result is None:
            return
        rec = self.camp_result.records[int(sel[0][1:])]
        self.set_fault_in_ui(rec.fault)
        self.nb.select(self.tab_state)
        self.do_inject()

    def export_csv(self):
        if self.camp_result is None:
            messagebox.showinfo("Export", "Run a campaign first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            self.camp_result.to_csv(path)


if __name__ == "__main__":
    FaultLab().mainloop()
