"""Write the classic-BPF seccomp filter that the coding sandbox passes to bubblewrap.

The filter allows every system call except a deny list taken from the exclusions in Docker's
default profile: calls that inspect other processes, load kernel code, change mounts or
namespaces, or open kernel interfaces a build never needs. Denied calls fail with EPERM.
`clone` with any namespace flag fails with EPERM. `clone3`, whose flags sit behind a
pointer that a classic filter cannot read, fails with ENOSYS, so libc falls back to
`clone`. A foreign architecture or an x32 call is killed. x86_64 only. This is a deny list,
not Docker's allowlist: bubblewrap's namespaces and the disabled user namespaces are the
primary boundary, and the filter narrows the kernel surface behind them.

usage: python3 -I seccomp.py <output file>
"""

import struct
import sys

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1
ENOSYS = 38
CLONE = 56
CLONE3 = 435
# CLONE_NEWTIME, NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID, NEWNET.
CLONE_NAMESPACES = 0x00000080 | 0x00020000 | 0x7E000000

# struct seccomp_data: nr at offset 0, arch at offset 4, args[0] (low half) at offset 16.
BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_JSET_K = 0x45
BPF_RET_K = 0x06

DENIED = {
    "acct": 163,
    "add_key": 248,
    "bpf": 321,
    "delete_module": 176,
    "finit_module": 313,
    "fsconfig": 431,
    "fsmount": 432,
    "fsopen": 430,
    "fanotify_init": 300,
    "fspick": 433,
    "init_module": 175,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "io_uring_setup": 425,
    "ioperm": 173,
    "iopl": 172,
    "kexec_file_load": 320,
    "kexec_load": 246,
    "kcmp": 312,
    "keyctl": 250,
    "mount": 165,
    "mount_setattr": 442,
    "move_mount": 429,
    "name_to_handle_at": 303,
    "open_by_handle_at": 304,
    "open_tree": 428,
    "perf_event_open": 298,
    "pivot_root": 155,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "ptrace": 101,
    "quotactl": 179,
    "reboot": 169,
    "request_key": 249,
    "setns": 308,
    "swapoff": 168,
    "swapon": 167,
    "syslog": 103,
    "umount2": 166,
    "unshare": 272,
    "userfaultfd": 323,
}


def program():
    """The filter as (code, jt, jf, k) instructions."""
    denied = sorted(DENIED.values())
    errno = SECCOMP_RET_ERRNO | EPERM
    insns = [
        (BPF_LD_W_ABS, 0, 0, 4),
        (BPF_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (BPF_LD_W_ABS, 0, 0, 0),
        (BPF_JGE_K, 0, 1, X32_SYSCALL_BIT),
        (BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
    ]
    insns += [
        (BPF_JEQ_K, 0, 1, CLONE3),
        (BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | ENOSYS),
        (BPF_JEQ_K, 0, 4, CLONE),
        (BPF_LD_W_ABS, 0, 0, 16),
        (BPF_JSET_K, 0, 1, CLONE_NAMESPACES),
        (BPF_RET_K, 0, 0, errno),
        (BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
    ]
    # Each denied number jumps forward to the shared ERRNO return after the ALLOW return.
    for index, nr in enumerate(denied):
        insns.append((BPF_JEQ_K, len(denied) - index, 0, nr))
    insns.append((BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    insns.append((BPF_RET_K, 0, 0, errno))
    return insns


def encode(insns):
    return b"".join(struct.pack("<HBBI", *insn) for insn in insns)


def main():
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(sys.argv[1], "wb") as out:
        out.write(encode(program()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
