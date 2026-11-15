"""Headless sanity tests for fi_engine.  Run:  python test_engine.py"""
import unittest

import fi_engine as E


def golden(name, cfg=None):
    p = E.assemble(E.SAMPLE_PROGRAMS[name], name)
    return p, cfg or E.CPUConfig(), E.run_program(p, cfg or E.CPUConfig())


class TestGolden(unittest.TestCase):
    def test_fibonacci(self):
        _, _, g = golden("Fibonacci (10 terms)")
        self.assertEqual(g.status, E.ST_HALTED)
        self.assertEqual(g.outputs, [0, 1, 1, 2, 3, 5, 8, 13, 21, 34])
        self.assertEqual(g.mem[0x100:0x10A], g.outputs)

    def test_sum_checksum(self):
        _, _, g = golden("Sum + XOR checksum + subroutine")
        self.assertEqual(g.status, E.ST_HALTED)
        self.assertEqual(g.outputs, [107, 15, 321])

    def test_bubble_sort(self):
        _, _, g = golden("Bubble sort (8 values)")
        self.assertEqual(g.status, E.ST_HALTED)
        self.assertEqual(g.outputs, [1, 3, 7, 19, 23, 42, 56, 88])

    def test_pin(self):
        _, _, g = golden("PIN check (glitch target)")
        self.assertEqual(g.outputs, [0])

    def test_all_samples_run_with_all_protections(self):
        cfg = E.CPUConfig(parity=True, watchdog=True, mpu=True)
        for name in E.SAMPLE_PROGRAMS:
            _, _, g = golden(name, cfg)
            self.assertEqual(g.status, E.ST_HALTED, name)


class TestAssembler(unittest.TestCase):
    def test_errors(self):
        for bad in ["FOO R1", "ADD R1", "LDI R0, 300", "JMP nowhere", "LD R0, R1"]:
            with self.assertRaises(E.AsmError):
                E.assemble(bad)

    def test_roundtrip_disasm(self):
        p = E.assemble("ADDI R3, -5\nLD R1, [R2]\nJNZ 0\nHALT")
        self.assertEqual([E.disasm(w) for w in p.words],
                         ["ADDI R3, -5", "LD R1, [R2]", "JNZ 0x00", "HALT"])


class TestFaults(unittest.TestCase):
    def test_instruction_skip_breaks_pin_check(self):
        p, cfg, g = golden("PIN check (glitch target)")
        f = E.FaultSpec("CTRL", "SKIP", cycle=6)          # skip the JNZ
        r = E.run_program(p, cfg, f)
        self.assertEqual(r.outputs, [0xAAAA])
        self.assertEqual(E.classify(g, r)[0], "SDC")

    def test_pc_flip_crashes(self):
        p, cfg, g = golden("Fibonacci (10 terms)")
        f = E.FaultSpec("PC", "FLIP", cycle=10, bit=12)   # PC jumps to 0x1xxx
        r = E.run_program(p, cfg, f)
        self.assertEqual(E.classify(g, r)[0], "CRASH")

    def test_parity_detects_register_flip(self):
        p, _, _ = golden("Fibonacci (10 terms)")
        f = E.FaultSpec("REG", "FLIP", cycle=6, index=0, bit=3)
        plain = E.run_program(p, E.CPUConfig(), f)
        prot_cfg = E.CPUConfig(parity=True)
        prot = E.run_program(p, prot_cfg, f)
        g = E.run_program(p, E.CPUConfig())
        self.assertEqual(E.classify(g, plain)[0], "SDC")
        self.assertEqual(E.classify(g, prot)[0], "DETECTED")

    def test_flip_in_unused_register_is_masked(self):
        p, cfg, g = golden("Fibonacci (10 terms)")
        f = E.FaultSpec("REG", "FLIP", cycle=3, index=7, bit=0)
        r = E.run_program(p, cfg, f)
        self.assertEqual(E.classify(g, r)[0], "MASKED")

    def test_stuck_at_already_present_never_activates(self):
        p, cfg, g = golden("Fibonacci (10 terms)")
        f = E.FaultSpec("REG", "STUCK0", cycle=0, index=7, bit=4, duration=0)
        r = E.run_program(p, cfg, f)
        self.assertFalse(r.activated)

    def test_stuck_at_pc_is_a_hang_or_crash(self):
        p, cfg, g = golden("Fibonacci (10 terms)")
        f = E.FaultSpec("REG", "STUCK1", cycle=0, index=4, bit=15, duration=0)  # loop counter
        r = E.run_program(p, cfg, f, max_cycles=E.hang_budget(g))
        self.assertIn(E.classify(g, r)[0], ("HANG", "SDC", "CRASH"))

    def test_watchdog(self):
        p = E.assemble("spin: JMP spin")
        r = E.run_program(p, E.CPUConfig(watchdog=True, wdt_limit=20), max_cycles=1000)
        self.assertEqual(r.status, E.ST_DETECTED)
        r = E.run_program(p, E.CPUConfig(watchdog=False), max_cycles=1000)
        self.assertEqual(r.status, E.ST_HANG)

    def test_mpu_blocks_code_write(self):
        src = "LDI R1, 5\nLDI R0, 1\nST R0, [R1]\nHALT"
        p = E.assemble(src)
        self.assertEqual(E.run_program(p, E.CPUConfig(mpu=True)).status, E.ST_CRASH)
        self.assertEqual(E.run_program(p, E.CPUConfig(mpu=False)).status, E.ST_HALTED)

    def test_memory_parity_on_code(self):
        p, _, _ = golden("Fibonacci (10 terms)")
        g = E.run_program(p, E.CPUConfig(parity=True))
        f = E.FaultSpec("MEM", "FLIP", cycle=2, index=8, bit=5)  # inside the loop body
        r = E.run_program(p, E.CPUConfig(parity=True), f)
        self.assertEqual(E.classify(g, r)[0], "DETECTED")

    def test_validation(self):
        with self.assertRaises(ValueError):
            E.FaultSpec("REG", "FLIP", 0, index=9).validate()
        with self.assertRaises(ValueError):
            E.FaultSpec("FLAGS", "FLIP", 0, bit=5).validate()
        with self.assertRaises(ValueError):
            E.FaultSpec("CTRL", "FLIP", 0).validate()


class TestCampaign(unittest.TestCase):
    def test_reproducible_and_sane(self):
        p, cfg, g = golden("Bubble sort (8 values)")
        cc = E.CampaignConfig(trials=150, seed=7, models=["FLIP", "STUCK0", "STUCK1"])
        a = E.run_campaign(p, cfg, g, cc)
        b = E.run_campaign(p, cfg, g, cc)
        self.assertEqual([r.outcome for r in a.records], [r.outcome for r in b.records])
        self.assertEqual(sum(a.counts().values()), 150)
        self.assertGreater(a.counts()["MASKED"], 0)
        self.assertGreater(sum(a.counts()[k] for k in ("SDC", "CRASH", "HANG")), 0)

    def test_parity_improves_detection(self):
        p, cfg, g = golden("Sum + XOR checksum + subroutine")
        cc = E.CampaignConfig(trials=200, seed=3, targets=["REG", "MEM"])
        base = E.run_campaign(p, cfg, g, cc).counts()
        pcfg = E.CPUConfig(parity=True)
        pg = E.run_program(p, pcfg)
        prot = E.run_campaign(p, pcfg, pg, cc).counts()
        self.assertEqual(base["DETECTED"], 0)
        self.assertGreater(prot["DETECTED"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
