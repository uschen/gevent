from test import test_support
import unittest
import select
import os
import sys

class SelectTestCase(unittest.TestCase):

    class Nope:
        pass

    class Almost:
        def fileno(self):
            return 'fileno'

    def test_error_conditions(self):
        self.assertRaises(TypeError, select.select, 1, 2, 3)
        self.assertRaises(TypeError, select.select, [self.Nope()], [], [])
        self.assertRaises(TypeError, select.select, [self.Almost()], [], [])
        self.assertRaises(TypeError, select.select, [], [], [], "not a number")

    def test_returned_list_identity(self):
        if sys.platform[:3] in ('win', 'mac', 'os2'):
            if test_support.verbose:
                print ("can't easily test on this system")
            return
        # See issue #8329
        r, w, x = select.select([], [], [], 1)
        self.assertFalse(r is w)
        self.assertFalse(r is x)
        self.assertFalse(w is x)

    def test_select(self):
        if sys.platform[:3] in ('win', 'mac', 'os2'):
            if test_support.verbose:
                print ("can't easily test on this system")
            return
        cmd = 'for i in 0 1 2 3 4 5 6 7 8 9; do echo testing...; sleep 0.1; done'
        p = os.popen(cmd, 'r')
        for tout in (0, 0.1, 0.2, 0.4, 0.8, 1.6) + (None,)*10:
            if test_support.verbose:
                print(('timeout =', tout))
            rfd, wfd, xfd = select.select([p], [], [], tout)
            if (rfd, wfd, xfd) == ([], [], []):
                continue
            if (rfd, wfd, xfd) == ([p], [], []):
                line = p.readline()
                if test_support.verbose:
                    print((repr(line)))
                if not line:
                    if test_support.verbose:
                        print ('EOF')
                    break
                continue
            self.fail('Unexpected return values from select():', rfd, wfd, xfd)
        p.close()


def test_main():
    test_support.run_unittest(SelectTestCase)
    test_support.reap_children()

if __name__ == "__main__":
    test_main()
