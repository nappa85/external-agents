import struct
import unittest

from support import seccomp


def run(program, nr, arch=seccomp.AUDIT_ARCH_X86_64, arg0=0):
    """Interpret the filter for one call, as the kernel does."""
    data = {0: nr, 4: arch, 16: arg0}
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == seccomp.BPF_LD_W_ABS:
            acc = data[k]
        elif code == seccomp.BPF_JEQ_K:
            pc += jt if acc == k else jf
        elif code == seccomp.BPF_JGE_K:
            pc += jt if acc >= k else jf
        elif code == seccomp.BPF_JSET_K:
            pc += jt if acc & k else jf
        elif code == seccomp.BPF_RET_K:
            return k
        pc += 1


class Filter(unittest.TestCase):
    program = seccomp.program()
    errno = seccomp.SECCOMP_RET_ERRNO | seccomp.EPERM

    def test_every_denied_call_fails_with_eperm(self):
        for name, nr in seccomp.DENIED.items():
            self.assertEqual(run(self.program, nr), self.errno, name)

    def test_ordinary_calls_are_allowed(self):
        for nr in (0, 1, 2, 9, 57, 59, 231, 257):  # read, write, open, mmap, fork, execve, …
            self.assertEqual(run(self.program, nr), seccomp.SECCOMP_RET_ALLOW, nr)

    def test_clone_is_allowed_for_threads_and_refused_for_namespaces(self):
        thread = 0x003D0F00  # CLONE_VM|FS|FILES|SIGHAND|THREAD|SYSVSEM|SETTLS|PARENT/CHILD_SETTID
        self.assertEqual(run(self.program, seccomp.CLONE, arg0=thread), seccomp.SECCOMP_RET_ALLOW)
        for flag in (
            0x80,
            0x20000,
            0x2000000,
            0x4000000,
            0x8000000,
            0x10000000,
            0x20000000,
            0x40000000,
        ):
            self.assertEqual(run(self.program, seccomp.CLONE, arg0=thread | flag), self.errno)

    def test_clone3_reports_enosys_so_libc_falls_back_to_clone(self):
        self.assertEqual(run(self.program, seccomp.CLONE3), seccomp.SECCOMP_RET_ERRNO | seccomp.ENOSYS)

    def test_x32_and_foreign_architectures_are_killed(self):
        self.assertEqual(run(self.program, seccomp.X32_SYSCALL_BIT | 1), seccomp.SECCOMP_RET_KILL_PROCESS)
        self.assertEqual(run(self.program, 0, arch=0x40000003), seccomp.SECCOMP_RET_KILL_PROCESS)

    def test_the_encoding_is_eight_bytes_per_instruction(self):
        blob = seccomp.encode(self.program)
        self.assertEqual(len(blob), 8 * len(self.program))
        self.assertEqual(struct.unpack("<HBBI", blob[:8]), self.program[0])


if __name__ == "__main__":
    unittest.main()
