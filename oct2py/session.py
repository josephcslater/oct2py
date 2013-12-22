"""
.. module:: session
   :synopsis: Main module for oct2py package.
              Contains the Octave session object Oct2Py

.. moduleauthor:: Steven Silvester <steven.silvester@ieee.org>

"""
from __future__ import print_function
import os
import re
import atexit
import doctest
import subprocess
import sys

try:
    from pexpect import spawn, ExceptionPexpect
except ImportError:
    spawn = None

from .matwrite import MatWrite
from .matread import MatRead
from .utils import get_nout, Oct2PyError, get_log
from .compat import unicode, input


class Oct2Py(object):
    """Manages an Octave session.

    Uses MAT files to pass data between Octave and Numpy.
    The function must either exist as an m-file in this directory or
    on Octave's path.
    The first command will take about 0.5s for Octave to load up.
    The subsequent commands will be much faster.

    You may provide a logger object for logging events, or the oct2py.get_log()
    default will be used.  Events will be logged as debug unless verbose is set
    when calling a command, then they will be logged as info.

    """
    def __init__(self, logger=None):
        """Start Octave and create our MAT helpers
        """
        if not logger is None:
            self.logger = logger
        else:
            self.logger = get_log()
        self.restart()

    def __enter__(self):
        '''Return octave object, restart session if necessary'''
        if not self._session:
            self.restart()
        return self

    def __exit__(self, type, value, traceback):
        '''Close session'''
        self.close()

    def close(self):
        """Closes this octave session and removes temp files
        """
        if self._session:
            self._session.close()
        self._session = None
        self._writer.remove_file()
        self._reader.remove_file()

    def run(self, script, **kwargs):
        """
        Run artibrary Octave code.

        Parameters
        -----------
        script : str
            Command script to send to Octave for execution.
        verbose : bool, optional
            Log Octave output at info level.

        Returns
        -------
        out : str
            Octave printed output.

        Raises
        ------
        Oct2PyError
            If the script cannot be run by Octave.

        Examples
        --------
        >>> from oct2py import octave
        >>> out = octave.run('y=ones(3,3)')
        >>> print(out)
        y =
        <BLANKLINE>
                1        1        1
                1        1        1
                1        1        1
        <BLANKLINE>
        >>> octave.run('x = mean([[1, 2], [3, 4]])')
        u'x =  2.5000'

        """
        # don't return a value from a script
        kwargs['nout'] = 0
        return self.call(script, **kwargs)

    def call(self, func, *inputs, **kwargs):
        """
        Call an Octave function with optional arguments.

        Parameters
        ----------
        func : str
            Function name to call.
        inputs : array_like
            Variables to pass to the function.
        nout : int, optional
            Number of output arguments.
            This is set automatically based on the number of
            return values requested (see example below).
            You can override this behavior by passing a
            different value.
        verbose : bool, optional
             Log Octave output at info level.

        Returns
        -------
        out : str or tuple
            If nout > 0, returns the values from Octave as a tuple.
            Otherwise, returns the output displayed by Octave.

        Raises
        ------
        Oct2PyError
            If the call is unsucessful.

        Examples
        --------
        >>> from oct2py import octave
        >>> b = octave.call('ones', 1, 2)
        >>> print(b)
        [[ 1.  1.]]
        >>> x, y = 1, 2
        >>> a = octave.call('zeros', x, y)
        >>> a
        array([[ 0.,  0.]])
        >>> U, S, V = octave.call('svd', [[1, 2], [1, 3]])
        >>> print((U, S, V))
        (array([[-0.57604844, -0.81741556],
               [-0.81741556,  0.57604844]]), array([[ 3.86432845,  0.        ],
               [ 0.        ,  0.25877718]]), array([[-0.36059668, -0.93272184],
               [-0.93272184,  0.36059668]]))

        """
        if self._first_run:
            self._first_run = False
            self._set_graphics_toolkit()

        verbose = kwargs.get('verbose', False)
        nout = kwargs.get('nout', get_nout())

        # handle references to script names - and paths to them
        if func.endswith('.m'):
            if os.path.dirname(func):
                self.addpath(os.path.dirname(func))
                func = os.path.basename(func)
            func = func[:-2]

        # these three lines will form the commands sent to Octave
        # load("-v6", "infile", "invar1", ...)
        # [a, b, c] = foo(A, B, C)
        # save("-v6", "outfile", "outvar1", ...)
        load_line = call_line = save_line = ''

        if nout:
            # create a dummy list of var names ("a", "b", "c", ...)
            # use ascii char codes so we can increment
            argout_list, save_line = self._reader.setup(nout)
            call_line = '[{0}] = '.format(', '.join(argout_list))
        if inputs:
            argin_list, load_line = self._writer.create_file(inputs)
            call_line += '{0}({1})'.format(func, ', '.join(argin_list))
        elif nout:
            # call foo() - no arguments
            call_line += '{0}()'.format(func)
        else:
            # run foo
            call_line += '{0}'.format(func)

        pre_call = '\nglobal __oct2py_figures = [];\n'
        post_call = ''

        if not nout and 'command' in kwargs and not '__ipy_figures' in func:
            if not call_line.endswith(')'):
                call_line += '();\n'
            post_call += '''
            # Save output of the last execution
                if exist("ans") == 1
                  _ = ans;
                else
                  _ = "__no_answer";
                end
            '''

        # do not interfere with octavemagic logic
        if not "DefaultFigureCreateFcn" in call_line:
            post_call += """
            for f = __oct2py_figures
                refresh(f);
            end"""

        # create the command and execute in octave
        cmd = [load_line, pre_call, call_line, post_call, save_line]
        resp = self._eval(cmd, verbose=verbose)

        if nout:
            return self._reader.extract_file(argout_list)
        elif 'command' in kwargs:
            try:
                ans = self.get('_')
            except KeyError:
                return
            # Unfortunately, Octave doesn't have a "None" object,
            # so we can't return any NaN outputs
            if isinstance(ans, (str, unicode)) and ans == "__no_answer":
                ans = None
            return ans
        else:
            return resp

    def put(self, names, var, verbose=False):
        """
        Put a variable into the Octave session.

        Parameters
        ----------
        names : str or list
            Name of the variable(s).
        var : object or list
            The value(s) to pass.

        Examples
        --------
        >>> from oct2py import octave
        >>> y = [1, 2]
        >>> octave.put('y', y)
        >>> octave.get('y')
        array([[1, 2]])
        >>> octave.put(['x', 'y'], ['spam', [1, 2, 3, 4]])
        >>> octave.get(['x', 'y'])
        (u'spam', array([[1, 2, 3, 4]]))

        """
        if isinstance(names, str):
            var = [var]
            names = [names]
        for name in names:
            if name.startswith('_'):
                raise Oct2PyError('Invalid name {0}'.format(name))
        _, load_line = self._writer.create_file(var, names)
        self._eval(load_line, verbose=verbose)

    def get(self, var, verbose=False):
        """
        Retrieve a value from the Octave session.

        Parameters
        ----------
        var : str
            Name of the variable to retrieve.

        Returns
        -------
        out : object
            Object returned by Octave.

        Raises:
          Oct2PyError
            If the variable does not exist in the Octave session.

        Examples:
          >>> from oct2py import octave
          >>> y = [1, 2]
          >>> octave.put('y', y)
          >>> octave.get('y')
          array([[1, 2]])
          >>> octave.put(['x', 'y'], ['spam', [1, 2, 3, 4]])
          >>> octave.get(['x', 'y'])
          (u'spam', array([[1, 2, 3, 4]]))

        """
        if isinstance(var, str):
            var = [var]
        # make sure the variable(s) exist
        for variable in var:
            if self._eval("exist {0}".format(variable),
                          verbose=False) == 'ans = 0' and not variable == '_':
                raise Oct2PyError('{0} does not exist'.format(variable))
        argout_list, save_line = self._reader.setup(len(var), var)
        self._eval(save_line, verbose=verbose)
        return self._reader.extract_file(argout_list)

    def lookfor(self, string, verbose=False):
        """
        Call the Octave "lookfor" command.

        Uses with the "-all" switch to search within help strings.

        Parameters
        ----------
        string : str
            Search string for the lookfor command.
        verbose : bool, optional
             Log Octave output at info level.

        Returns
        -------
        out : str
            Output from the Octave lookfor command.

        """
        return self.run('lookfor -all {0}'.format(string), verbose=verbose)

    def _eval(self, cmds, verbose=True, log=True):
        """
        Perform raw Octave command.

        This is a low-level command, and should not technically be used
        directly.  The API could change. You have been warned.

        Parameters
        ----------
        cmds : str or list
            Commands(s) to pass directly to Octave.
        verbose : bool, optional
             Log Octave output at info level.

        Returns
        -------
        out : str
            Results printed by Octave.

        Raises
        ------
        Oct2PyError
            If the command(s) fail.

        """
        if not self._session:
            raise Oct2PyError('No Octave Session')
        if isinstance(cmds, str):
            cmds = [cmds]
        if verbose and log:
            [self.logger.info(line) for line in cmds]
        elif log:
            [self.logger.debug(line) for line in cmds]
        return self._session.evaluate(cmds, verbose, log, self.logger)

    def _make_octave_command(self, name, doc=None):
        """Create a wrapper to an Octave procedure or object

        Adapted from the mlabwrap project

        """
        def octave_command(*args, **kwargs):
            """ Octave command """
            kwargs['nout'] = get_nout()
            kwargs['verbose'] = kwargs.get('verbose', False)
            self._eval('clear {}'.format(name), log=False, verbose=False)
            kwargs['command'] = True
            return self.call(name, *args, **kwargs)
        # convert to ascii for pydoc
        try:
            doc = doc.encode('ascii', 'replace').decode('ascii')
        except UnicodeDecodeError:
            pass
        octave_command.__doc__ = "\n" + doc
        octave_command.__name__ = name
        return octave_command

    def _get_doc(self, name):
        """
        Get the documentation of an Octave procedure or object.

        Parameters
        ----------
        name : str
            Function name to search for.

        Returns
        -------
        out : str
          Documentation string.

        Raises
        ------
        Oct2PyError
           If the procedure or object does not exist.

        """
        exist = self._eval('exist {0}'.format(name), log=False, verbose=False)
        if exist == 'ans = 0':
            msg = 'Name: "%s" does not exist on the Octave session path'
            raise Oct2PyError(msg % name)
        doc = 'No documentation for %s' % name
        try:
            doc = self._eval('help {0}'.format(name), log=False, verbose=False)
        except Oct2PyError as e:
            if 'syntax error' in str(e):
                raise(e)
            try:
                doc = self._eval('type {0}'.format(name), log=False, 
                                 verbose=False)
                doc = '\n'.join(doc.splitlines()[:3])
            except Oct2PyError as e:
                pass
        return doc

    def __getattr__(self, attr):
        """Automatically creates a wapper to an Octave function or object.

        Adapted from the mlabwrap project.

        """
        # needed for help(Oct2Py())
        if attr == '__name__':
            return super(Oct2Py, self).__getattr__(attr)
        elif attr == '__file__':
            return __file__
        if re.search(r'\W', attr):  # work around ipython <= 0.7.3 bug
            raise Oct2PyError(
                "Attributes don't look like this: {0}".format(attr))
        if attr.startswith('_'):
            raise Oct2PyError(
                "Octave commands do not start with _: {0}".format(attr))
        # print_ -> print
        if attr[-1] == "_":
            name = attr[:-1]
        else:
            name = attr
        doc = self._get_doc(name)
        octave_command = self._make_octave_command(name, doc)
        #!!! attr, *not* name, because we might have python keyword name!
        setattr(self, attr, octave_command)
        return octave_command

    def _set_graphics_toolkit(self):
        try:
            self._eval("graphics_toolkit('gnuplot')", False)
        except Oct2PyError:  # pragma: no cover
            pass
        # set up the plot renderer
        self.run("""
            global __oct2py_figures = [];
            page_screen_output(0);

            function fig_create(src, event)
              global __oct2py_figures;
              __oct2py_figures(size(__oct2py_figures) + 1) = src;
            end

            set(0, 'DefaultFigureCreateFcn', @fig_create);
        """)
        self._graphics_toolkit = 'gnuplot'

    def restart(self):
        '''Restart an Octave session in a clean state
        '''
        self._session = _Session()
        self._first_run = True
        self._graphics_toolkit = None
        self._reader = MatRead()
        self._writer = MatWrite()

    def interact(self, prompt='octave> ', banner=None):
        """Interact with the Octave session directly"""
        self._session.interact(prompt, banner)


class _Session(object):
    '''Low-level session Octave session interaction
    '''
    def __init__(self):
        self.proc = self.start()
        self.stdout = sys.stdout
        atexit.register(self.close)

    def start(self):
        """
        Start an octave session in a subprocess.

        Returns
        =======
        out : fid
            File descriptor for the Octave subprocess

        Raises
        ======
        Oct2PyError
            If the session is not opened sucessfully.

        Notes
        =====
        Options sent to Octave: -q is quiet startup, --braindead is
        Matlab compatibilty mode.

        """
        global spawn
        if spawn:
            try:
                return spawn('octave', ['-q', '--braindead'])
            except Exception:
                spawn = None
                return self.start_subprocess()
        else:
            return self.start_subprocess()

    def start_subprocess(self):
        """Start octave using a subprocess (no tty support)"""
        errmsg = ('\n\nPlease install GNU Octave and put it in your path\n')
        ON_POSIX = 'posix' in sys.builtin_module_names
        kwargs = dict(stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, close_fds=ON_POSIX)
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs['startupinfo'] = startupinfo
        try:
            proc = subprocess.Popen(['octave', '-q', '--braindead'], **kwargs)
        except OSError:  # pragma: no cover
            raise Oct2PyError(errmsg)
        else:
            return proc

    def evaluate(self, cmds, verbose=True, log=True, logger=None):
        '''Perform the low-level interaction with an Octave Session
        '''
        if not self.proc:
            raise Oct2PyError('Session Closed, try a restart()')
        # use ascii code 21 to signal an error and 3
        # to signal end of text
        lines = ['try', 'disp(char(3))', '\n'.join(cmds), 'disp(char(3))',
                 'catch', 'disp(lasterr())', 'disp(char(21))',
                 'end', '']
        if len(cmds) == 5:
            main_line = cmds[2].strip()
        else:
            main_line = '\n'.join(cmds)
        output = '\n'.join(lines)
        pattern = re.compile(r'(keyboard\(.*\);?|keyboard;?)')
        keyboard = re.search(pattern, output)
        if keyboard:
            name = self._make_interaction_file('octave> ')
            output = re.sub(pattern, ';' + name + ';', output, count=1)
            output = re.sub(pattern, ';', output)
        self.write(output)
        resp = self.expect(['\x03', 'syntax error'])
        if resp.endswith('syntax error'):
            while not '^' in resp:
                resp += self.expect('\n')
            self.handle_syntax_error(resp, main_line)
            return
        if not spawn:
            try:
                self.proc.stdin.flush()
            except OSError:  # pragma: no cover
                pass
        resp = []
        syntax_error = False
        while 1:
            line = self.expect(['\n', 'debug> ', 'octave> ']).rstrip()
            if line == '\x03':
                break
            elif line == '\x15':
                msg = ('Oct2Py tried to run:\n"""\n{0}\n"""\nOctave returned:\n{1}'
                       .format(main_line, '\n'.join(resp)))
                raise Oct2PyError(msg)
            elif line in ['debug>', 'octave>']:
                msg = 'Entering Octave Debug Prompt...\n%s ' % line
                self.stdout.write(msg)
                cont = self._interact(line + ' ')
                self.write('clear _\n')
                resp = resp[:-4]
                self.expect('\n')
                if not cont:
                    return
                else:
                    continue
            if "syntax error" in line:
                syntax_error = True
            elif syntax_error and "^" in line:
               resp.append(line)
               self.handle_syntax_error(''.join(resp), main_line)
               return
            if verbose and logger:
                logger.info(line)
            elif log and logger:
                logger.debug(line)
            if resp or line:
                resp.append(line)
        if keyboard:
            os.remove(self.interaction_file)
        return '\n'.join(resp)

    def handle_syntax_error(self, resp, main_line):
        """Handle an Octave syntax error"""
        if not spawn:
            msg = 'Octave Syntax Error:\n%s' % resp
            msg += '\nSession Closed by Octave'
            self.close()
            raise Oct2PyError(msg)
        elif resp[-1] == '>>> catch':
            msg = ('Oct2Py tried to run:\n"""\n%s\n"""\n'
                   'Octave returned Syntax Error' % main_line)
            raise Oct2PyError(msg)
        else:
            msg = ('Oct2Py tried to run:\n"""\n{0}\n"""\nOctave returned:\n{1}'
               .format(main_line, resp))
            raise Oct2PyError(msg)

    def expect(self, strings):
        """Look for a string or strings in the incoming data"""
        if not isinstance(strings, list):
            strings = [strings]
        line = ''
        if spawn:
            self.proc.expect(strings)
            line = self.proc.before + self.proc.after
            return line.decode('utf-8')
        else:
            line = ''
            while 1:
                line += self.read()
                for string in strings:
                    if line.endswith(string):
                        return line

    def interact(self, prompt='octave> ', banner=None):
        """Interact with the Octave session directly"""
        if not os.name == 'nt' and not spawn:
            raise Oct2PyError('Please install pexpect for interaction capability')
        if not banner:
            banner = ('Starting Octave Interactive Prompt...\n'
                     'Type "return" when finished\n')
        if not banner.endswith('\n'):
            banner += '\n'
        self.stdout.write(banner)
        name = self._make_interaction_file(prompt)
        self.write('%s;\n' % name)
        self._find_prompt(prompt, False)
        self.read(4)
        self._find_prompt(prompt)
        os.remove(self.interaction_file)
        self._interact(prompt)

    def _make_interaction_file(self, prompt):
        pwd = self.get_pwd()
        path = '%s/__oct2py_interact.m' % pwd
        with open(path, 'wb') as fid:
            msg = 'keyboard("%s")\n' % prompt
            fid.write(msg.encode('utf-8'))
        self.interaction_file = path
        return '__oct2py_interact'

    def get_pwd(self):
        """Get the present working directory of the session"""
        pwd = self.evaluate(['pwd'], False, False)
        return pwd.splitlines()[-1][6:]

    def _find_prompt(self, prompt='debug> ', disp=True):
        """Look for the prompt in the Octave output, print chars if disp"""
        output = ''
        while 1:
            output += self.read()
            if output.endswith(prompt):
                if disp:
                    self.stdout.write(output)
                return

    def read(self, n=1):
        """Read characters from the process with utf-8 encoding"""
        if spawn:
            chars = self.proc.read(n)
        else:
            chars = self.proc.stdout.read(n)
        return chars.decode('utf-8', 'replace')

    def write(self, message):
        """Write a message to the process using utf-8 encoding"""
        if spawn:
            self.proc.write(message.encode('utf-8'))
        else:
            self.proc.stdin.write(message.encode('utf-8'))

    def _interact(self, prompt='debug> '):
        """Manage an Octave Debug Prompt interaction"""
        if not os.name == 'nt' and not spawn:
            raise Oct2PyError('Please install pexpect for interaction capability')
        while 1:
            inp = input() + '\n'
            if inp == 'exit\n':
                inp = 'return\n'
            self.write('disp(char(3));' + inp)
            if inp in ['dbcont\n', 'return\n']:
                return True
            if inp in ['dbquit\n']:
                return False
            self.expect('\x03\r\n')
            self._find_prompt(prompt)

    def close(self):
        '''Cleanly close an Octave session
        '''
        try:
            self.write('exit\n')
        except (IOError, AttributeError):
            pass
        try:
            self.proc.terminate()
        except (OSError, AttributeError):  # pragma: no cover
            pass
        self.proc = None


def _test():  # pragma: no cover
    """Run the doctests for this module.
    """
    print('Starting doctest')
    doctest.testmod()
    print('Completed doctest')


if __name__ == "__main__":  # pragma: no cover
    _test()
