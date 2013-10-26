from __future__ import print_function
import unittest
import os
import sys
import subprocess
import fcntl
import errno
import time
import signal
import atexit
import traceback
from contextlib import contextmanager
try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO

class BufferingBase(object):

    BUFFSIZE = 8192
    def __init__(self, fd):
        self.buff = StringIO()
        self.fd = fd

    def read(self):
        """
        Read any available data fd. Does NOT block.
        """
        try:
            while 1:
                data = os.read(self.fd, self.BUFFSIZE)
                if not data:
                    break
                self.buff.write(data.decode('utf8'))
        except OSError as e:
            if e.errno not in (
                errno.EAGAIN, errno.EWOULDBLOCK,
                errno.EINPROGRESS
            ):
                print("Failed to read from %s: %s" % (self.fd, e))
        return self.buff.getvalue()

    def reset(self):
        self.buff = StringIO()

class TestProcess(BufferingBase):
    def __init__(self, *args):
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ,
            bufsize=1,
        )
        fd = self.proc.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        super(TestProcess, self).__init__(fd)

    @property
    def is_alive(self):
        return self.proc.poll() is None

    def signal(self, sig):
        self.proc.send_signal(sig)

    def __enter__(self):
        return self

    def __exit__(self, exc_type=None, exc_value=None, exc_traceback=None):
        try:
            self.proc.send_signal(signal.SIGINT)
            for _ in range(5):
                time.sleep(0.2)
                if self.proc.poll() is not None:
                    self.proc.terminate()
            for _ in range(10):
                time.sleep(0.1)
                if self.proc.poll() is not None:
                    return
            print('KILLED %s' % self, file=sys.stderr)
            self.proc.kill()
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise
        finally:
            self.read()
            if self.proc.stdout:
                self.proc.stdout.close()
            if self.proc.stderr:
                self.proc.stderr.close()
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait() # reap the zombie
    close = __exit__

class TestSocket(BufferingBase):
    BUFFSIZE = 8192
    def __init__(self, sock):
        sock.setblocking(0)
        self.sock = sock
        super(TestSocket, self).__init__(sock.fileno())

    def __enter__(self):
        return self

    def __exit__(self, exc_type=None, exc_value=None, exc_traceback=None):
        try:
            self.sock.close()
        except OSError as exc:
            if exc.errno not in (errno.EBADF, errno.EBADFD):
                raise
    close = __exit__

class ProcessTestCase(unittest.TestCase):

    def wait_for_strings(self, cb, seconds, *strings):
        """
        This checks that *string appear in cb(), IN THE GIVEN ORDER !
        """
        buff = '<UNINITIALIZED>'

        for _ in range(int(seconds * 20)):
            time.sleep(0.05)
            buff = cb()
            check_strings = list(strings)
            check_strings.reverse()
            for line in buff.splitlines():
                if not check_strings:
                    break
                while check_strings and check_strings[-1] in line:
                    check_strings.pop()
            if not check_strings:
                return

        raise AssertionError("Waited %0.2fsecs but %s did not appear in output in the given order !" % (
            seconds, strings
        ))

    @contextmanager
    def dump_on_error(self, cb):
        try:
            yield
        except Exception:
            print("*********** OUTPUT ***********")
            print(cb())
            print("******************************")
            raise

_COV = None
def restart_coverage():
    global _COV
    try:
        from coverage.control import coverage
        from coverage.collector import Collector
    except ImportError:
        _COV = None
        return
    if _COV:
        _COV.save()
        _COV.stop()
    if Collector._collectors:
        Collector._collectors[-1].stop()
    _COV = _COV
    if _COV:
        _COV = coverage(auto_data=True, data_suffix=True, timid=False, include=['src/*'])
        _COV.start()

        @atexit.register
        def cleanup():
            if _COV.collector._collectors:
                _COV.stop()
            _COV.save()

def monkeypatch(mod, what):
    """
    Patch function named <what> from module <mod> to run the decorated function after <what> completes.

    Eg::

        @monkeypatch(os, 'forkpty')
        def patched_forkpty(pid_fd):
            pid, fd = pid_fd
            if not pid:
                maybe_enable_coverage()
            return pid, fd
    """

    old = getattr(mod, what)
    def decorator(func):
        def patch():
            ret = old()
            try:
                func(ret)
            except:
                traceback.print_exc()
            return ret
        setattr(mod, what, patch)
    return decorator

def setup_coverage(env_var="WITH_COVERAGE"):
    """
    Patch fork and forkpty to restart coverage measurement after fork. Expects to have a environment variable named WITH_COVERAGE set to a
    non-empty value.
    """
    if os.environ.get(env_var): # don't even bother if not set
        restart_coverage()

        @monkeypatch(os, 'fork')
        def patched_fork(pid):
            if not pid and:
                restart_coverage()
            return pid

        @monkeypatch(os, 'forkpty')
        def patched_forkpty(pid_fd):
            pid, fd = pid_fd
            if not pid:
                restart_coverage()
            return pid, fd
