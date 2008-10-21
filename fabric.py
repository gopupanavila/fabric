#!/usr/bin/env python -i

# Fabric - Pythonic remote deployment tool.
# Copyright (C) 2008  Christian Vest Hansen
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import datetime
import getpass
import os
import os.path
import pwd
import re
import signal
import subprocess
import sys
import threading
import time
import types
from functools import partial, wraps

try:
    import paramiko as ssh
except ImportError:
    print("Error: paramiko is a required module. Please install it:")
    print("  $ sudo easy_install paramiko")
    sys.exit(1)

__version__ = '0.0.9'
__author__ = 'Christian Vest Hansen'
__author_email__ = 'karmazilla@gmail.com'
__url__ = 'http://www.nongnu.org/fab/'
__license__ = 'GPL-2'
__about__ = '''\
   Fabric v. %(fab_version)s, Copyright (C) 2008 %(fab_author)s.
   Fabric comes with ABSOLUTELY NO WARRANTY.
   This is free software, and you are welcome to redistribute it
   under certain conditions. Please reference full license for details.
'''

ENV = {
    'fab_version': __version__,
    'fab_author': __author__,
    'fab_mode': 'rolling',
    'fab_port': 22,
    'fab_user': pwd.getpwuid(os.getuid())[0],
    'fab_password': None,
    'fab_pkey': None,
    'fab_key_filename': None,
    'fab_new_host_key': 'accept',
    'fab_shell': '/bin/bash -l -c "%s"',
    'fab_timestamp': datetime.datetime.utcnow().strftime('%F_%H-%M-%S'),
    'fab_print_real_sudo': False,
    'fab_fail': 'abort',
}

CONNECTIONS = []
COMMANDS = {}
OPERATIONS = {}
STRATEGIES = {}
_LAZY_FORMAT_SUBSTITUTER = re.compile(r'\$\((?P<var>\w+?)\)')

_LOADED_FABFILES = set()
_CALLED_COMMANDS = set()

#
# Compatibility fixes
#
if hasattr(str, 'partition'):
    partition = str.partition
else:
    def partition(txt, sep):
        idx = txt.find(sep)
        if idx == -1:
            return txt, '', ''
        else:
            return (txt[:idx], sep, txt[idx + len(sep):])

#
# Helper decorators:
#
def new_registering_decorator(registry):
    def registering_decorator(first_arg=None):
        if callable(first_arg):
            registry[first_arg.__name__] = first_arg
            return first_arg
        else:
            def sub_decorator(f):
                registry[first_arg] = f
                return f
            return sub_decorator
    return registering_decorator
command = new_registering_decorator(COMMANDS)
operation = new_registering_decorator(OPERATIONS)
strategy = new_registering_decorator(STRATEGIES)

def run_per_host(op_fn):
    def wrapper(*args, **kwargs):
        if not CONNECTIONS:
            _connect()
        _on_hosts_do(op_fn, *args, **kwargs)
    wrapper.__doc__ = op_fn.__doc__
    wrapper.__name__ = op_fn.__name__
    return wrapper

#
# Standard fabfile operations:
#
@operation
def set(**variables):
    """
    Set a number of Fabric environment variables.
    
    `set()` takes a number of keyword arguments, and defines or updates the
    variables that correspond to each keyword with the respective value.
    
    The values can be of any type, but strings are used for most variables.
    If the value is a string and contain any eager variable references, such as
    `%(fab_user)s`, then these will be expanded to their corresponding value.
    Lazy references, those beginning with a `$` rather than a `%`, will not be
    expanded.
    
    Example:
    
        set(fab_user='joe.shmoe', fab_mode='rolling')
    
    """
    for k, v in variables.items():
        if isinstance(v, types.StringTypes):
            ENV[k] = (v % ENV)
        else:
            ENV[k] = v

@operation
def get(name, otherwise=None):
    """
    Get the value of a given Fabric environment variable.
    
    If the variable isn't found, then this operation returns the
    value of the `otherwise` parameter, which is None unless set.
    
    """
    return ENV.get(name, otherwise)

@operation
def getAny(*names):
    """
    Given a list of variable names as parameters, get the value of the first
    of these variables that is actually defined (and does not resolve to
    boolean `False`), or `None`.
    
    Example:
    
        getAny('hostname', 'ipv4', 'ipv6', 'ip', 'address')
    
    """
    for name in names:
        value = ENV.get(name)
        if value:
            return value
    # Implicit return value of None here if no names found.

@operation
def require(*varnames, **kwargs):
    """
    Make sure that certain environment variables are available.
    
    The `varnames` parameters are one or more strings that names the variables
    to check for.
    
    Two other optional kwargs are supported:
    
     * `used_for` is a string that gets injected into, and then printed, as
       something like this string: `"This variable is used for %s"`.
     * `provided_by` is a list of strings that name commands which the user
       can run in order to satisfy the requirement, or references to the
       actual command functions them selves.
    
    If the required variables are not found in the current environment, then 
    the operation is stopped and Fabric halts.
    
    Examples:

        # One variable name
        require('project_name',
            used_for='finding the target deployment dir.',
            provided_by=['staging', 'production'],
        )
    
        # Multiple variable names
        require('project_name', 'install_dir', provided_by=[stg, prod])

    """
    if all([var in ENV for var in varnames]):
        return
    if len(varnames) == 1:
        vars_msg = "a %r variable." % varnames[0]
    else:
        vars_msg = "the variables %s." % ", ".join(
                ["%r" % vn for vn in varnames])
    print(
        ("The '%(fab_cur_command)s' command requires " + vars_msg) % ENV
    )
    if 'used_for' in kwargs:
        print("This variable is used for %s" % _lazy_format(
            kwargs['used_for']))
    if 'provided_by' in kwargs:
        print("Get the variable by running one of these commands:")
        to_s = lambda obj: getattr(obj, '__name__', str(obj))
        provided_by = [to_s(obj) for obj in kwargs['provided_by']]
        print('\t' + ('\n\t'.join(provided_by)))
    sys.exit(1)

@operation
def prompt(varname, msg, validate=None, default=None):
    """
    Display a prompt to the user and store the input in the given variable.
    If the variable already exists, then it is not prompted for again. (Unless
    it doesn't validate, see below.)
    
    The `validate` parameter is a callable that raises an exception on invalid
    inputs and returns the input for storage in `ENV`.
    
    It may process the input and convert it to a different type, as in the
    second example below.
    
    If `validate` is instead given as a string, it will be used as a regular
    expression against which the input must match.
    
    If validation fails, the exception message will be printed and prompt will
    be called repeatedly until a valid value is given.
    
    Example:
    
        # Simplest form:
        prompt('environment', 'Please specify target environment')
        
        # With default:
        prompt('dish', 'Specify favorite dish', default='spam & eggs')
        
        # With validation, i.e. require integer input:
        prompt('nice', 'Please specify process nice level', validate=int)
        
        # With validation against a regular expression:
        prompt('release', 'Please supply a release name',
                validate=r'^\w+-\d+(\.\d+)?$')
    
    """
    value = None
    if varname in ENV and ENV[varname] is not None:
        value = ENV[varname]
    
    if callable(default):
        default = default()
    
    try:
        default_str = default and (" [%s]" % str(default).strip()) or ""
        prompt_msg = _lazy_format("%s%s: " % (msg.strip(), default_str))
        
        if isinstance(validate, types.StringTypes):
            validate = RegexpValidator(validate)
        
        while True:
            value = value or raw_input(prompt_msg) or default
            if callable(validate):
                try:
                    value = validate(value)
                except Exception, e:
                    value = None
                    print e.message
            if value:
                break
        
        set(**{varname: value})
    except EOFError:
        return

@operation
@run_per_host
def put(host, client, env, localpath, remotepath, **kwargs):
    """
    Upload a file to the current hosts.
    
    The `localpath` parameter is the relative or absolute path to the file on
    your localhost that you wish to upload to the `fab_hosts`.
    The `remotepath` parameter is the destination path on the individual
    `fab_hosts`, and relative paths are relative to the fab_user's home
    directory.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        put('bin/project.zip', '/tmp/project.zip')
    
    """
    localpath = _lazy_format(localpath, env)
    remotepath = _lazy_format(remotepath, env)
    if not os.path.exists(localpath):
        return False
    ftp = client.open_sftp()
    print("[%s] put: %s -> %s" % (host, localpath, remotepath))
    ftp.put(localpath, remotepath)
    return True

@operation
@run_per_host
def download(host, client, env, remotepath, localpath, **kwargs):
    """
    Download a file from the remote hosts.
    
    The `remotepath` parameter is the relative or absolute path to the files
    to download from the `fab_hosts`. The `localpath` parameter will be
    suffixed with the individual hostname from which they were downloaded, and
    the downloaded files will then be stored in those respective paths.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        set(fab_hosts=['node1.cluster.com', 'node2.cluster.com'])
        download('/var/log/server.log', 'server.log')
    
    The above code will produce two files on your local system, called
    `server.log.node1.cluster.com` and `server.log.node2.cluster.com`
    respectively.
    
    """
    ftp = client.open_sftp()
    localpath = _lazy_format(localpath) + '.' + host
    remotepath = _lazy_format(remotepath)
    print("[%s] download: %s <- %s" % (host, localpath, remotepath))
    ftp.get(remotepath, localpath)
    return True

@operation
@run_per_host
def run(host, client, env, cmd, **kwargs):
    """
    Run a shell command on the current fab_hosts.
    
    The provided command is executed with the permissions of fab_user, and the
    exact execution environ is determined by the `fab_shell` variable.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        run("ls")
    
    """
    cmd = _lazy_format(cmd, env)
    real_cmd = env['fab_shell'] % cmd.replace('"', '\\"')
    real_cmd = _escape_bash_specialchars(real_cmd)
    if not _confirm_proceed('run', host, kwargs):
        return False
    print("[%s] run: %s" % (host, cmd))
    chan = client._transport.open_session()
    chan.exec_command(real_cmd)
    bufsize = -1
    stdin = chan.makefile('wb', bufsize)
    stdout = chan.makefile('rb', bufsize)
    stderr = chan.makefile_stderr('rb', bufsize)
    
    out_th = _start_outputter("[%s] out" % host, stdout)
    err_th = _start_outputter("[%s] err" % host, stderr)
    status = chan.recv_exit_status()
    chan.close()
    return status == 0

@operation
@run_per_host
def sudo(host, client, env, cmd, **kwargs):
    """
    Run a sudo (root privileged) command on the current hosts.
    
    The provided command is executed with root permissions, provided that
    `fab_user` is in the sudoers file in the remote host. The exact execution
    environ is determined by the `fab_shell` variable - the `sudo` part is
    injected into this variable.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        sudo("install_script.py")
    
    """
    cmd = _lazy_format(cmd, env)
    passwd = env['fab_password']
    sudo_cmd = passwd and "sudo -S " or "sudo "
    real_cmd = env['fab_shell'] % (sudo_cmd + cmd.replace('"', '\\"'))
    cmd = env['fab_print_real_sudo'] and real_cmd or cmd
    if not _confirm_proceed('sudo', host, kwargs):
        return False # TODO: should we return False in fail??
    print("[%s] sudo: %s" % (host, cmd))
    chan = client._transport.open_session()
    real_cmd = _escape_bash_specialchars(real_cmd)
    chan.exec_command(real_cmd)
    bufsize = -1
    stdin = chan.makefile('wb', bufsize)
    stdout = chan.makefile('rb', bufsize)
    stderr = chan.makefile_stderr('rb', bufsize)
    if passwd:
        stdin.write(env['fab_password'])
        stdin.write('\n')
        stdin.flush()
    out_th = _start_outputter("[%s] out" % host, stdout)
    err_th = _start_outputter("[%s] err" % host, stderr)
    status = chan.recv_exit_status()
    chan.close()
    return status == 0

@operation
def local(cmd, **kwargs):
    """
    Run a command locally.
    
    This operation is essentially `os.system()` except that variables are
    expanded prior to running.
    
    May take an additional 'fail' keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        local("make clean dist", fail='abort')
    
    """
    # we don't need _escape_bash_specialchars for local execution
    final_cmd = _lazy_format(cmd)
    print("[localhost] run: " + final_cmd)
    retcode = subprocess.call(final_cmd, shell=True)
    if retcode != 0:
        _fail(kwargs, "Local command failed:\n" + _indent(final_cmd))

@operation
def local_per_host(cmd, **kwargs):
    """
    Run a command locally, for every defined host.
    
    Like the `local()` operation, this is pretty similar to `os.system()`, but
    with this operation, the command is executed (and have its variables
    expanded) for each host in `fab_hosts`.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        local_per_host("scp -i login.key stuff.zip $(fab_host):stuff.zip")
    
    """
    _check_fab_hosts()
    con_envs = [con.get_env() for con in CONNECTIONS]
    if not con_envs:
        # we might not have connected yet
        for hostname in ENV['fab_hosts']:
            env = {}
            env.update(ENV)
            env['fab_host'] = hostname
            con_envs.append(env)
    for env in con_envs:
        final_cmd = _lazy_format(cmd, env)
        print(_lazy_format("[localhost/$(fab_host)] run: " + final_cmd, env))
        retcode = subprocess.call(final_cmd, shell=True)
        if retcode != 0:
            _fail(kwargs, "Local command failed:\n" + _indent(final_cmd))

@operation
def load(filename, **kwargs):
    """
    Load up the given fabfile.
    
    This loads the fabfile specified by the `filename` parameter into fabric
    and make its commands and other functions available in the scope of the 
    current fabfile.
    
    May take an additional `fail` keyword argument with one of these values:
    
     * ignore - do nothing on failure
     * warn - print warning on failure
     * abort - terminate fabric on failure
    
    Example:
    
        load("conf/production-settings.py")
    
    """
    if not os.path.exists(filename):
        _fail(kwargs, "Load failed:\n" + _indent(
            "File not found: " + filename))
        return
    
    if filename in _LOADED_FABFILES:
        return
    _LOADED_FABFILES.add(filename)
    
    execfile(filename)
    for name, obj in locals().items():
        if not name.startswith('_') and isinstance(obj, types.FunctionType):
            COMMANDS[name] = obj
        if not name.startswith('_'):
            __builtins__[name] = obj

@operation
def upload_project(**kwargs):
    """
    Uploads the current project directory to the connected hosts.
    
    This is a higher-level convenience operation that basically 'tar' up the
    directory that contains your fabfile (presumably it is your project
    directory), uploads it to the `fab_hosts` and 'untar' it.
    
    This operation expects the tar command-line utility to be available on your
    local machine, and it also expects your system to have a `/tmp` directory
    that is writeable.
    
    Unless something fails half-way through, this operation will make sure to
    delete the temporary files it creates.
    
    """
    tar_file = "/tmp/fab.%(fab_timestamp)s.tar" % ENV
    cwd_name = os.getcwd().split(os.sep)[-1]
    local("tar -czf %s ." % tar_file, **kwargs)
    put(tar_file, cwd_name + ".tar.gz", **kwargs)
    local("rm -f " + tar_file, **kwargs)
    run("tar -xzf " + cwd_name, **kwargs)
    run("rm -f " + cwd_name + ".tar.gz", **kwargs)

@operation
def call_once(command, *args, **kwargs):
    """
    Calls the supplied command unless it has already been called.
    """
    # TODO: *commands; and invoke via _execute_commands?
    # TODO: what, if any, messages?
    if command in _CALLED_COMMANDS:
        print "Already invoked %s, skipping." % command.__name__
        return
    print "Invoking %s..." % command.__name__
    _CALLED_COMMANDS.add(command)
    command(*args, **kwargs)

#
# Standard Fabric commands:
#
@command("help")
def _help(**kwargs):
    """
    Display Fabric usage help, or help for a given command.
    
    You can provide help with a parameter and get more detailed help for a
    specific command. For instance, to learn more about the list command, you
    could run `fab help:list`.
    
    If you are developing your own fabfile, then you might also be interested
    in learning more about operations. You can do this by running help with the
    `op` parameter set to the name of the operation you would like to learn
    more about. For instance, to learn more about the `run` operation, you
    could run `fab help:op=run`.
    
    Lastly, you can also learn more about a certain strategy with the `strg`
    and `strategy` parameters: `fab help:strg=rolling`.
    
    """
    if kwargs:
        for k, v in kwargs.items():
            if k in COMMANDS:
                _print_help_for_in(k, COMMANDS)
            elif k in OPERATIONS:
                _print_help_for_in(k, OPERATIONS)
            elif k in ['op', 'operation']:
                _print_help_for_in(kwargs[k], OPERATIONS)
            elif k in ['strg', 'strategy']:
                _print_help_for_in(kwargs[k], STRATEGIES)
            else:
                _print_help_for(k, None)
    else:
        print("""
    Fabric is a simple pythonic remote deployment tool.
    
    Type `fab list` to get a list of available commands.
    Type `fab help:help` to get more information on how to use the built in
    help.
    
    """)

@command("about")
def _print_about(**kwargs):
    "Display Fabric version, warranty and license information"
    print(__about__ % ENV)

@command("list")
def _list_commands(**kwargs):
    """
    Display a list of commands with descriptions.
    
    By default, the list command prints a list of available commands, with a
    short description (if one is available). However, the list command can also
    print a list of available operations if you provide it with the `ops` or
    `operations` parameters, or it can print strategies with the `strgs` and
    `strategies` parameters.
    
    """
    if kwargs:
        for k, v in kwargs.items():
            if k in ['cmds', 'commands']:
                print("Available commands are:")
                _list_objs(COMMANDS)
            elif k in ['ops', 'operations']:
                print("Available operations are:")
                _list_objs(OPERATIONS)
            elif k in ['strgs', 'strategies']:
                print("Available strategies are:")
                _list_objs(STRATEGIES)
            else:
                print("Don't know how to list '%s'." % k)
                print("Try one of these instead:")
                print(_indent('\n'.join([
                    'cmds', 'commands',
                    'ops', 'operations',
                    'strgs', 'strategies',
                ])))
                sys.exit(1)
    else:
        print("Available commands are:")
        _list_objs(COMMANDS)

@command("set")
def _set(**kwargs):
    """
    Set a Fabric variable.
    
    Example:
    
        $fab set:fab_user=billy,other_var=other_value
    """
    for k, v in kwargs.items():
        ENV[k] = (v % ENV)

@command("shell")
def _shell(**kwargs):
    """
    Start an interactive shell connection to the specified hosts.
    
    Optionally takes a list of hostnames as arguments, if Fabric is, by
    the time this command runs, not already connected to one or more
    hosts. If you provide hostnames and Fabric is already connected, then
    Fabric will, depending on `fab_fail`, complain and abort.
    
    The `fab_fail` variable can be overwritten with the `set` command, or
    by specifying an additional `fail` argument.
    
    Examples:
    
        $fab shell
        $fab shell:localhost,127.0.0.1
        $fab shell:localhost,127.0.0.1,fail=warn
    
    """
    # expect every arg w/o a value to be a hostname
    hosts = filter(lambda k: not kwargs[k], kwargs.keys())
    if hosts:
        if CONNECTIONS:
            _fail(kwargs, "Already connected to predefined fab_hosts.")
        set(fab_hosts = hosts)
    def lines():
        try:
            while True:
                yield raw_input("fab> ")
        except EOFError:
            # user pressed ctrl-d
            print
    for line in lines():
        if line == 'exit':
            break
        elif line.startswith('sudo '):
            sudo(line[5:], fail='warn')
        else:
            run(line, fail='warn')

#
# Standard strategies:
#
@strategy("fanout")
def _fanout_strategy(fn, *args, **kwargs):
    """
    A strategy that executes on all hosts in parallel.
    
    THIS STRATEGY IS CURRENTLY BROKEN!
    
    """
    err_msg = "The $(fab_current_operation) operation failed on $(fab_host)"
    threads = []
    for host_conn in CONNECTIONS:
        env = host_conn.get_env()
        env['fab_current_operation'] = fn.__name__
        host = env['fab_host']
        client = host_conn.client
        def functor():
            _try_run_operation(fn, host, client, env, *args, **kwargs)
        thread = threading.Thread(None, functor)
        thread.setDaemon(True)
        threads.append(thread)
    map(threading.Thread.start, threads)
    map(threading.Thread.join, threads)

@strategy("rolling")
def _rolling_strategy(fn, *args, **kwargs):
    """One-at-a-time fail-fast strategy."""
    err_msg = "The $(fab_current_operation) operation failed on $(fab_host)"
    for host_conn in CONNECTIONS:
        env = host_conn.get_env()
        env['fab_current_operation'] = fn.__name__
        host = env['fab_host']
        client = host_conn.client
        _try_run_operation(fn, host, client, env, *args, **kwargs)

#
# Utility decorators:
#
# TODO: register these (for the help system).

def _new_operator_decorator(operator, *use_args, **use_kwargs):
    def decorator(command):
        @wraps(command)
        def decorated(*args, **kwargs):
            operator(*use_args, **use_kwargs)
            command(*args, **kwargs)
        return decorated
    return decorator

requires = partial(_new_operator_decorator, require)
depends = partial(_new_operator_decorator, call_once)

#
# Internal plumbing:
#

class RegexpValidator(object):
    def __init__(self, pattern):
        self.regexp = re.compile(pattern)
    def __call__(self, value):
        regexp = self.regexp
        if value is None or not regexp.match(value):
            raise ValueError("Malformed value %r. Must match r'%s'." %
                    (value, regexp.pattern))
        return value

class HostConnection(object):
    """
    A connection to an SSH host - wraps an SSHClient.
    
    Instances of this class populate the CONNECTIONS list.
    """
    def __init__(self, hostname, port, global_env, user_local_env):
        self.global_env = global_env
        self.user_local_env = user_local_env
        self.host_local_env = {
            'fab_host': hostname,
            'fab_port': port,
        }
        self.client = None
    def get_env(self):
        "Create a new environment that is the union of local and global envs."
        env = dict(self.global_env)
        env.update(self.user_local_env)
        env.update(self.host_local_env)
        return env
    def connect(self):
        env = self.get_env()
        new_host_key = env['fab_new_host_key']
        client = ssh.SSHClient()
        client.load_system_host_keys()
        if new_host_key == 'accept':
            client.set_missing_host_key_policy(ssh.AutoAddPolicy())
        try:
            self._do_connect(client, env)
        except (ssh.AuthenticationException, ssh.SSHException):
            PASS_PROMPT = \
                "Password for $(fab_user)@$(fab_host)$(fab_passprompt_suffix)"
            if 'fab_password' in env and env['fab_password']:
                env['fab_passprompt_suffix'] = " [Enter for previous]: "
            else:
                env['fab_passprompt_suffix'] = ": "
            connected = False
            password = None
            while not connected:
                try:
                    password = getpass.getpass(_lazy_format(PASS_PROMPT, env))
                    env['fab_password'] = password
                    self._do_connect(client, env)
                    connected = True
                except ssh.AuthenticationException:
                    print("Bad password.")
                    env['fab_passprompt_suffix'] = ": "
                except (EOFError, TypeError):
                    # ctrl-D or ctrl-C on password prompt
                    print
                    sys.exit(0)
            self.host_local_env['fab_password'] = password
            self.user_local_env['fab_password'] = password
        self.client = client
    def disconnect(self):
        if self.client:
            self.client.close()
    def _do_connect(self, client, env):
        host = env['fab_host']
        port = env['fab_port']
        username = env['fab_user']
        password = env['fab_password']
        pkey = env['fab_pkey']
        key_filename = env['fab_key_filename']
        client.connect(host, port, username, password, pkey, key_filename)
    def __str__(self):
        return self.host_local_env['fab_host']

def _indent(text, level=4):
    "Indent all lines in text with 'level' number of spaces, default 4."
    return '\n'.join(((' ' * level) + line for line in text.splitlines()))

def _print_help_for(name, doc):
    "Output a pretty-printed help text for the given name & doc"
    default_help_msg = '* No help-text found.'
    msg = doc or default_help_msg
    lines = msg.splitlines()
    # remove leading blank lines
    while lines and lines[0].strip() == '':
        lines = lines[1:]
    # remove trailing blank lines
    while lines and lines[-1].strip() == '':
        lines = lines[:-1]
    if lines:
        msg = '\n'.join(lines)
        if not msg.startswith('    '):
            msg = _indent(msg)
        print("Help for '%s':\n%s" % (name, msg))
    else:
        print("No help message found for '%s'." % name)

def _print_help_for_in(name, dictionary):
    "Print a pretty help text for the named function in the dict."
    if name in dictionary:
        _print_help_for(name, dictionary[name].__doc__)
    else:
        _print_help_for(name, None)

def _list_objs(objs):
    max_name_len = reduce(lambda a, b: max(a, len(b)), objs.keys(), 0)
    cmds = objs.items()
    cmds.sort(lambda x, y: cmp(x[0], y[0]))
    for name, fn in cmds:
        print '  ', name.ljust(max_name_len),
        if fn.__doc__:
            print ':', filter(None, fn.__doc__.splitlines())[0].strip()
        else:
            print

def _check_fab_hosts():
    "Check that we have a fab_hosts variable, and complain if it's missing."
    if 'fab_hosts' not in ENV:
        print("Fabric requires a fab_hosts variable.")
        print("Please set it in your fabfile.")
        print("Example: set(fab_hosts=['node1.com', 'node2.com'])")
        sys.exit(1)
    if len(ENV['fab_hosts']) == 0:
        print("The fab_hosts list was empty.")
        print("Please specify some hosts to connect to.")
        sys.exit(1)

def _connect():
    "Populate CONNECTIONS with HostConnection instances as per fab_hosts."
    _check_fab_hosts()
    signal.signal(signal.SIGINT, lambda: _disconnect() and sys.exit(0))
    global CONNECTIONS
    def_port = ENV['fab_port']
    username = ENV['fab_user']
    fab_hosts = ENV['fab_hosts']
    user_envs = {}
    host_connections_by_user = {}
    
    # grok fab_hosts into who connects to where
    for host in fab_hosts:
        if '@' in host:
            user, _, host_and_port = partition(host, '@')
        else:
            user, host_and_port = None, host
        hostname, _, port = partition(host_and_port, ':')
        user = user or username
        port = int(port or def_port)
        if user is not '' and user not in user_envs:
            user_envs[user] = {'fab_user': user}
        conn = HostConnection(hostname, port, ENV, user_envs[user])
        if user not in host_connections_by_user:
            host_connections_by_user[user] = [conn]
        else:
            host_connections_by_user[user].append(conn)
    
    # Print and establish connections
    for user, host_connections in host_connections_by_user.iteritems():
        user_env = dict(ENV)
        user_env.update(user_envs[user])
        print(_lazy_format("Logging into the following hosts as $(fab_user):",
            user_env))
        print(_indent('\n'.join(map(str, host_connections))))
        map(HostConnection.connect, host_connections)
        CONNECTIONS += host_connections
    set(fab_connected=True)

def _disconnect():
    "Disconnect all clients."
    global CONNECTIONS
    map(HostConnection.disconnect, CONNECTIONS)
    CONNECTIONS = []

def _lazy_format(string, env=ENV):
    "Do recursive string substitution of ENV vars - both lazy and eager."
    if string is None:
        return None
    env = dict([(k, str(v)) for k, v in env.items()])
    def replacer_fn(match):
        var = match.group('var')
        if var in env:
            return _lazy_format(env[var] % env, env)
        else:
            return match.group(0)
    return re.sub(_LAZY_FORMAT_SUBSTITUTER, replacer_fn, string % env)

def _escape_bash_specialchars(cmd):
    return cmd.replace("$", "\\$")

def _on_hosts_do(fn, *args, **kwargs):
    """
    Invoke the given function with hostname and client parameters in
    accord with the current fab_mode strategy.
    
    fn should be a callable taking these parameters:
        hostname : str
        client : paramiko.SSHClient
        *args
        **kwargs
    
    """
    strategy = ENV['fab_mode']
    if strategy in STRATEGIES:
        strategy_fn = STRATEGIES[strategy]
        strategy_fn(fn, *args, **kwargs)
    else:
        print("Unsupported fab_mode: %s" % strategy)
        print("Supported modes are: %s" % (', '.join(STRATEGIES.keys())))
        sys.exit(1)

def _try_run_operation(fn, host, client, env, *args, **kwargs):
    """
    Used by strategies to attempt the execution of an operation, and handle
    any failures appropriately.
    """
    err_msg = "The $(fab_current_operation) operation failed on $(fab_host)"
    success = False
    try:
        success = fn(host, client, env, *args, **kwargs)
    except SystemExit:
        raise
    except BaseException, e:
        _fail(kwargs, err_msg + ':\n' + _indent(str(e)), env)
    if not success:
        _fail(kwargs, err_msg + '.', env)

def _confirm_proceed(exec_type, host, kwargs):
    if 'confirm' in kwargs:
        infotuple = (exec_type, host, _lazy_format(kwargs['confirm']))
        question = "Confirm %s for host %s: %s [yN] " % infotuple
        answer = raw_input(question)
        return answer and answer in 'yY'
    return True

def _fail(kwargs, msg, env=ENV):
    # Get failure code
    codes = {
        'ignore': (1, ''),
        'warn': (2, 'Warning: '),
        'abort': (3, 'Error: '),
    }
    code, msg_prefix = codes[env['fab_fail']]
    if 'fail' in kwargs:
        code, msg_prefix = codes[kwargs['fail']]
    # If warn or above, print message
    if code > 1:
        print(msg_prefix + _lazy_format(msg, env))
        # If abort, also exit
        if code > 2:
            sys.exit(1)


def _start_outputter(prefix, channel):
    def outputter():
        line = channel.readline()
        while line:
            print("%s: %s" % (prefix, line)),
            line = channel.readline()
    thread = threading.Thread(None, outputter, prefix)
    thread.setDaemon(True)
    thread.start()
    return thread

def _pick_fabfile():
    "Figure out what the fabfile is called."
    guesses = ['fabfile', 'Fabfile', 'fabfile.py', 'Fabfile.py']
    options = filter(os.path.exists, guesses)
    if options:
        return options[0]
    else:
        return guesses[0] # load() will barf for us...

def _load_default_settings():
    "Load user-default fabric settings from ~/.fabric"
    # TODO: http://mail.python.org/pipermail/python-list/2006-July/393819.html
    cfg = os.path.expanduser("~/.fabric")
    if os.path.exists(cfg):
        comments = lambda s: s and not s.startswith("#")
        settings = filter(comments, open(cfg, 'r'))
        settings = [(k.strip(), v.strip()) for k, _, v in
            [partition(s, '=') for s in settings]]
        ENV.update(settings)

def _parse_args(args):
    cmds = []
    for cmd in args:
        cmd_args = {}
        if ':' in cmd:
            cmd, cmd_str_args = cmd.split(':', 1)
            for cmd_arg_kv in cmd_str_args.split(','):
                k, _, v = partition(cmd_arg_kv, '=')
                cmd_args[k] = (v % ENV) or k
        cmds.append((cmd, cmd_args))
    return cmds

def _validate_commands(cmds):
    if not cmds:
        print("No commands given.")
        _list_commands()
    else:
        for cmd in cmds:
            if not cmd[0] in COMMANDS:
                print("No such command: %s" % cmd[0])
                sys.exit(1)

def _execute_commands(cmds):
    for cmd, args in cmds:
        ENV['fab_cur_command'] = cmd
        print("Running %s..." % cmd)
        if args is not None:
            args = dict(zip(args.keys(), map(_lazy_format, args.values())))
        COMMANDS[cmd](**(args or {}))

def main():
    args = sys.argv[1:]
    try:
        try:
            print("Fabric v. %(fab_version)s." % ENV)
            _load_default_settings()
            fabfile = _pick_fabfile()
            load(fabfile, fail='warn')
            commands = _parse_args(args)
            _validate_commands(commands)
            _execute_commands(commands)
        finally:
            _disconnect()
        print("Done.")
    except SystemExit:
        # a number of internal functions might raise this one.
        raise
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(1)
    except:
        sys.excepthook(*sys.exc_info())
        # we might leave stale threads if we don't explicitly exit()
        sys.exit(1)
    sys.exit(0)
    

