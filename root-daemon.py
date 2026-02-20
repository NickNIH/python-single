#!/usr/bin/env python3
import argparse
import errno
import logging
import os
import pathlib
import pwd
import socket
import subprocess
import sys
from typing import Callable, Optional, NoReturn, TypedDict, Union
import fcntl

SOCKET_DIR_DEFAULT = pathlib.Path('/run')
MAX_LINE_LENGTH = 10 * 1024  # 10 KiB limit for a single request line.

DESCRIPTION = """Root daemon providing privileged operations via a Unix domain socket."""
EPILOG = """You can ensure one (and only one) instance of this daemon is running by adding a root
cron job like `* * * * * flock -n /run/root-daemon.me.lock "$HOME/bin/root-daemon.py" --user me`"""

def make_argparser():
    parser = argparse.ArgumentParser(add_help=False, description=DESCRIPTION, epilog=EPILOG)
    options = parser.add_argument_group('Options')
    options.add_argument('-u', '--user',
        help='The user who should be able to write to the socket. Default: the current user.')
    options.add_argument('-s', '--socket-path',
        help='Unix domain socket path to listen to. By default, it will be '
            f'{SOCKET_DIR_DEFAULT}/root-daemon.[user].sock, where [user] is the given --user.')
    options.add_argument('-t', '--timeout', type=float, default=60.0,
        help='Per-connection timeout in seconds. Use 0 to disable (default: %(default)s).')
    options.add_argument('-h', '--help', action='help',
        help='Print this argument help text and exit.')
    logs = parser.add_argument_group('Logging')
    logs.add_argument('-l', '--log', type=argparse.FileType('w'), default=sys.stderr,
        help='Print log messages to this file instead of to stderr. Warning: Will overwrite the file.')
    volume = logs.add_mutually_exclusive_group()
    volume.add_argument('-q', '--quiet', dest='volume', action='store_const', const=logging.CRITICAL,
        default=logging.WARNING)
    volume.add_argument('-v', '--verbose', dest='volume', action='store_const', const=logging.INFO)
    volume.add_argument('-D', '--debug', dest='volume', action='store_const', const=logging.DEBUG)
    return parser


def main(*argv: str) -> Optional[int]:

    parser = make_argparser()
    args = parser.parse_args(argv[1:])

    logging.basicConfig(stream=args.log, level=args.volume, format='%(message)s')

    if args.user is None:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError as error:
            fail(f'Unable to determine current user for uid {os.getuid()}: {error}')
    else:
        user = args.user

    if args.socket_path is None:
        socket_path = SOCKET_DIR_DEFAULT / f'root-daemon.{user}.sock'
    else:
        socket_path = pathlib.Path(args.socket_path)

    run_server(socket_path, user, args.timeout)
    return 0


def run_server(socket_path: pathlib.Path, user: str, timeout: float) -> None:
    """Listen on a Unix domain socket and dispatch privileged operations."""

    try:
        pwent = pwd.getpwnam(user)
    except KeyError as error:
        fail(f"Unknown user {user!r}: {error}")

    uid = pwent.pw_uid
    gid = pwent.pw_gid

    socket_path_str = str(socket_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            os.unlink(socket_path_str)
        except FileNotFoundError:
            pass

        server.bind(socket_path_str)
        # Set ownership and restrict access: owner read/write, group read/write.
        try:
            os.chown(socket_path_str, uid, gid)
        except PermissionError as error:
            fail(f'Error: unable to chown socket {socket_path_str!r} to {uid}:{gid}: {error}')

        os.chmod(socket_path_str, 0o660)
        server.listen(5)
        logging.info(f'Listening on socket {socket_path_str!r}.')

        while True:
            conex, _ = server.accept()
            with conex:
                handle_connection(conex, timeout)
    finally:
        try:
            server.close()
        except Exception:
            pass
        try:
            os.unlink(socket_path_str)
        except FileNotFoundError:
            pass


def handle_connection(
    conex: socket.socket, timeout: float, max_line_length: int = MAX_LINE_LENGTH
) -> None:
    """Handle a single client connection.

        Protocol: one line per request, UTF-8 text:
            OP_NAME\tARG1\tARG2 ...\n
        A single response line is written back, as a tab-delimited record:
            status\tmessage\n
        where `status` is an all-lowercase status token (e.g. "success" or
        "error"), and `message` is a human-readable description.
    """

    if timeout > 0:
        try:
            conex.settimeout(timeout)
        except OSError as error:
            logging.error(f'Error: unable to set timeout on client connection: {error}')
            return

    reader = conex.makefile(mode='r', encoding='utf-8', errors='replace')
    writer = conex.makefile(mode='w', encoding='utf-8', errors='replace')
    try:
        try:
            line = reader.readline(max_line_length + 1)
        except TimeoutError as error:
            logging.error(f'Connection timed out while waiting for request: {error}')
            return
        if not line:
            return
        if len(line) > max_line_length and not line.endswith('\n'):
            logging.error(
                'Received overlong request line (> %d bytes); closing connection.',
                max_line_length
            )
            return
        text = line.strip()
        if not text:
            logging.error('Received empty request line from client; closing connection.')
            return

        # Request fields are tab-delimited: OP_NAME\tARG1\tARG2 ...
        fields = text.split('\t')
        op_name = fields[0].lower()
        op_args = fields[1:]

        op_info = OPERATIONS.get(op_name)
        if op_info is None:
            status = 'error'
            message = f'Unknown operation {op_name!r}'
        else:
            handler = op_info['handler']
            try:
                success, message = handler(op_args)
                status = 'success' if success else 'error'
            except Exception as error:  # noqa: BLE001
                logging.error(f'Error while handling operation {op_name!r}: {error}')
                status = 'error'
                message = 'internal error while handling request'

        response = f'{status}\t{message}'

        if not response.endswith('\n'):
            response += '\n'
        writer.write(response)
        writer.flush()
    finally:
        try:
            reader.close()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass


def do_shutdown(args: list[str]) -> tuple[bool,str]:
    """Power off the system immediately using systemctl.
    Any arguments are currently ignored.
    """

    cmd = ('systemctl', 'poweroff')
    logging.info('$ '+' '.join(cmd))
    try:
        # This assumes a systemd-based system.
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as error:
        logging.error(f'Error: systemctl poweroff failed: {error}')
        return False, 'shutdown failed'
    except FileNotFoundError as error:
        logging.error(f'Error: systemctl not found: {error}')
        return False, 'systemctl not found'
    return True, 'shutdown initiated'

Handler = Callable[[list[str]], tuple[bool, str]]


class Operation(TypedDict):
    handler: Handler
    help: str


OPERATIONS: dict[str, Operation] = {
    'shutdown': {
        'handler': do_shutdown,
        'help': 'Power off the system immediately.',
    },
}


def fail(error: Union[str,BaseException], code: int = 1) -> NoReturn:
    if __name__ == '__main__':
        logging.critical(f'Error: {error}')
        sys.exit(code)
    elif isinstance(error, BaseException):
        raise error
    else:
        raise RuntimeError(error)


if __name__ == '__main__':
    try:
        sys.exit(main(*sys.argv))
    except BrokenPipeError:
        pass
