import os


class ProcFcntl:
    F_GETPATH = None

    @staticmethod
    def fcntl(fd, _cmd, _arg):
        return os.fsencode(os.readlink(f"/proc/self/fd/{fd}"))
