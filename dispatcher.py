#!/usr/bin/env python3
import argparse
import collections
import datetime
import io
import logging
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import yaml
assert sys.version_info.major >= 3, 'Python 3 required'

DATA_DIR_DEFAULT = pathlib.Path('~/.local/share/nbsdata/dispatcher').expanduser()
SILENCE_FILE = DATA_DIR_DEFAULT/'SILENCE'
PERIODS = collections.OrderedDict(
  (
    ('min', {'dt':lambda dt: dt.minute}),
    ('hr',  {'dt':lambda dt: dt.hour}),
    ('dom', {'dt':lambda dt: dt.day}),
    ('mon', {'dt':lambda dt: dt.month}),
    ('week',{'dt':lambda dt: dt.weekday()+1})
  )
)

DESCRIPTION = """Take actions based on the content of a simple input file."""
USER_AGENT = 'dispatcher/1.0'


def make_argparser():
  parser = argparse.ArgumentParser(add_help=False, description=DESCRIPTION)
  options = parser.add_argument_group('Options')
  options.add_argument('input', nargs='?',
    help='Input file or URL. Omit to read from stdin.')
  options.add_argument('-u', '--url', action='store_true',
    help='Force the input argument to be treated as a URL instead of a file.')
  options.add_argument('-c', '--config', type=argparse.FileType('r'),
    help='Config file for parameters and options.')
  options.add_argument('-w', '--whitelist', type=pathlib.Path, action='append', default=[],
    help='Allow accessing files under this directory. Can give multiple directories by giving this '
      'option multiple times.')
  options.add_argument('-p', '--precision', type=int,
    help='Time precision of execution. How many minutes since the last time this was executed? '
      'Required for any command with a ?when parameter. Currently only applied to the ?when hour '
      'and minute.')
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


def main(argv):

  parser = make_argparser()
  args = parser.parse_args(argv[1:])

  logging.basicConfig(stream=args.log, level=args.volume, format='%(message)s')

  settings = {'whitelist':[], 'data_dir':DATA_DIR_DEFAULT}
  if args.config:
    read_config(args.config, settings)
  for path in args.whitelist:
    settings['whitelist'].append(path.resolve())

  input_type = None
  if SILENCE_FILE.exists():
    logging.warning(
      'Warning: Silence file %r exists. Treating input arg as a file path.', str(SILENCE_FILE)
    )
    input_type = 'file'
  elif args.url:
    input_type = 'url'
  input_stream = get_input_stream(args.input, input_type)
  seen_commands = set()

  for lines in chunk_input(input_stream):
    # Parse the chunk.
    try:
      command, chunk_args, params, content = parse_chunk(lines)
    except ValueError as error:
      logging.error(error)
      continue
    # Include static params, but allow them to be overridden by ones from the current chunk.
    for key, value in settings.items():
      if key not in params:
        params[key] = value
    # Allow deactivating commands easily.
    if 'pass' in params:
      continue
    # Postpone commands according to 'when' parameter.
    if 'when' in params:
      if not execute_now(params['when'], args.precision):
        continue
    seen_commands.add(command)
    # Execute the command.
    fxn = COMMANDS[command]['execute']
    fxn(chunk_args, content, params)

  # Call any "absent" handlers for commands that were not seen.
  for name, info in COMMANDS.items():
    if name in seen_commands:
      continue
    on_absent = info.get('on_absent')
    if on_absent is not None:
      on_absent(settings)


def read_config(config_file, params):
  data = yaml.safe_load(config_file)
  if 'whitelist' in data:
    for path_str in data['whitelist']:
      path = pathlib.Path(path_str)
      if not path.is_absolute():
        logging.error(f'Error: Config file whitelist path not absolute: {str(path)!r}')
      params['whitelist'].append(path)


def get_input_stream(input_arg, input_type):
  """Return a file-like object to read `input_arg` from.
  If `input_arg` is None or '-', read from stdin.
  If `input_type` is 'url', treat `input_arg` as a URL. If `input_type` is 'file', treat `input_arg`
  as a file.
  Otherwise, use `input_arg` as a local path when it exists; if it does not exist but it looks like
  a URL, treat it as a URL."""

  if input_arg is None or input_arg == '-':
    return sys.stdin

  url = None

  if input_type == 'url':
    url = input_arg
  else:
    path = pathlib.Path(input_arg)
    if path.is_file():
      try:
        return path.open('r')
      except OSError as error:
        logging.error('Error: Failed to open input file %r: %s', str(path), error)
        raise
    elif input_type == 'file':
      # If we're treating this as a file but it doesn't exist, fail loudly.
      raise FileNotFoundError(f'Input file {str(path)!r} not found.')
    # File does not exist. If it looks like a URL, treat it as one.
    if input_arg.startswith(('http://', 'https://')):
      url = input_arg
    else:
      # Behave like the old FileType('r'): try to open and fail loudly.
      try:
        return path.open('r')
      except OSError as error:
        logging.error('Error: Failed to open input file %r: %s', str(path), error)
        raise

  try:
    request = urllib.request.Request(url, headers={'User-Agent':USER_AGENT})
    with urllib.request.urlopen(request) as response:
      encoding = response.headers.get_content_charset()
      if encoding is None:
        encoding = 'utf-8'
      text = response.read().decode(encoding, errors='replace')
  except (urllib.error.URLError, ValueError) as error:
    logging.error(f'Error: Failed to read input URL {url!r}: {error}')
    raise
  return io.StringIO(text)


def chunk_input(lines):
  chunk_lines = []
  for line_raw in lines:
    line = line_raw.rstrip('\r\n')
    if line.startswith('!'):
      if chunk_lines:
        yield chunk_lines
      chunk_lines = []
    chunk_lines.append(line)
  if chunk_lines:
    yield chunk_lines


def parse_chunk(chunk_lines):
  if len(chunk_lines) <= 0:
    raise ValueError('Received a chunk with no lines.')
  first_line = chunk_lines[0]
  command, args = parse_command(first_line)
  content = []
  params = {}
  for line in chunk_lines[1:]:
    if not content and line.startswith('?'):
      param_type, param = parse_params(line)
      params[param_type] = param
    else:
      content.append(line)
  return command, args, params, content


def parse_command(command_line):
  assert command_line.startswith('!'), command_line
  fields = command_line[1:].split()
  command = fields[0]
  args = fields[1:]
  if command not in COMMANDS:
    raise ValueError(f'Unrecognized command {command!r} in line {command_line!r}')
  return command, args


def parse_params(params_line):
  assert params_line.startswith('?'), params_line
  fields = params_line[1:].split()
  param_type = fields[0]
  param_args = fields[1:]
  if param_type == 'when':
    return 'when', parse_when_param(param_args)
  if param_type == 'pass':
    return 'pass', True
  else:
    raise ValueError(f'Invalid parameter type {param_type!r}')


def parse_when_param(args):
  if len(args) != len(PERIODS):
    raise ValueError(
      f'Wrong number of arguments to when parameter (saw {len(args)}, need {len(PERIODS)}).'
    )
  time_spec = {}
  for period, arg in zip(PERIODS, args):
    if arg == '*':
      value = None
    else:
      try:
        value = int(arg)
      except ValueError as error:
        error.args = (f'Invalid when parameter {arg!r} (not an integer or *)',)
        raise error
    time_spec[period] = value
  return time_spec


def execute_now(time_spec, precision):
  if precision is None:
    logging.error('Error: Encountered #?when parameter, but no --precision given.')
    return None
  now = datetime.datetime.now()
  # If the time_spec includes a day of any kind, are we on the right day?
  for period, spec_value in time_spec.items():
    if period in ('min', 'hr'):
      continue
    elif spec_value is None:
      continue
    dt_converter = PERIODS[period]['dt']
    current_value = dt_converter(now)
    if current_value != spec_value:
      return False
  # How many minutes after the time_spec are we executing?
  spec_hr = time_spec['hr']
  if spec_hr is None:
    spec_hr = now.hour
  spec_min = time_spec['min']
  if spec_min is None:
    spec_min = now.minute
  spec_minutes = spec_hr*60 + spec_min
  now_minutes = now.hour*60 + now.minute
  diff = now_minutes - spec_minutes
  if diff < 0:
    diff += 24*60
  logging.debug(f'Debug: Got a time diff of {diff} from {now_minutes} - {spec_minutes}')
  if diff < 0:
    raise ValueError(
      f'time_spec seems to be > 24 hrs after now? (time_spec: {time_spec!r}, now: {now!r})'
    )
  if diff > precision:
    return False
  else:
    return True


def do_echo(args, content, params):
  print(*args)


def do_cat(args, content, params):
  whitelist = params.get('whitelist', ())
  for path_str in args:
    path = pathlib.Path(path_str).resolve()
    if not in_whitelist(path, whitelist):
      logging.error(f'Error: Path not in whitelist: {str(path)!r}')
      continue
    if not path.parent.is_dir():
      logging.error(f'Error: Directory containing {str(path)!r} not found.')
      continue
    try:
      with path.open('w') as file:
        for line in content:
          print(line, file=file)
    except OSError as error:
      logging.error(f'Error: Failed writing to file {str(path)!r}: {error}')


def do_shutdown(args, content, params):
  shutdown_path = params['data_dir']/"shutdown.tsv"
  delay_seconds = None

  if args:
    # Check if there's already a file noting the shutdown request.
    # If not, create one with the time it was noticed and the wait time.
    # If it already exists, check if we're past the shutdown time.
    # If so, continue out of this conditional (toward the shutdown).
    try:
      delay_seconds = parse_time(args[0])
    except ValueError:
      logging.error(
        f'Error: Invalid arg to shutdown command (delay not MM or HH:MM): {args[0]!r}'
      )
      return False

    now_ts = int(time.time())
    noticed_ts = None
    existing_delay = None
    new_request = False

    if shutdown_path.exists():
      try:
        with shutdown_path.open('r') as file:
          line = file.readline().strip()
        if line:
          noticed_str, delay_str = line.split('\t')
          noticed_ts = int(noticed_str)
          existing_delay = int(delay_str)
        else:
          logging.warning(
            'Warning: Empty shutdown request file '
            f'{str(shutdown_path)!r}. Overwriting with new request.'
          )
      except (OSError, ValueError) as error:
        logging.error(
          f'Error: Failed to read existing shutdown request file {str(shutdown_path)!r}: {error}'
        )

    # If there was no valid existing request, or the existing request used a
    # different delay, treat this as a new request and overwrite the file.
    if noticed_ts is None or existing_delay != delay_seconds:
      new_request = True
      noticed_ts = now_ts
      try:
        shutdown_path.parent.mkdir(parents=True, exist_ok=True)
        with shutdown_path.open('w') as file:
          print(noticed_ts, delay_seconds, sep='\t', file=file)
      except OSError as error:
        logging.error(
          'Error: Failed to write shutdown request file '
          f'{str(shutdown_path)!r}: {error}'
        )

    # Show a warning that it will shut down after the requested delay.
    # That should take care of the case where I boot the computer back up but haven't yet taken
    # down the shutdown request.
    elapsed = now_ts - noticed_ts
    if elapsed < delay_seconds:
      remaining = max(int(delay_seconds - elapsed), 0)
      # Always log the alert; only show GUI alerts for new requests.
      show_shutdown_alert(remaining, log=True, gui=new_request)
      return

  # Either no wait was requested or we've passed the shutdown time.

  show_shutdown_alert(0, log=True, gui=True)

  # Best-effort cleanup of the shutdown request file.
  try:
    if shutdown_path.exists():
      shutdown_path.unlink()
  except OSError as error:
    logging.error(f'Error: Failed to remove shutdown request file {str(shutdown_path)!r}: {error}')

  shutdown()


def on_absent_shutdown(settings):
  """Handle cases where no shutdown command is seen in the input.
  If there is a pending shutdown request file, remove it so that a
  later shutdown request is always treated as new. """
  shutdown_path = settings['data_dir']/"shutdown.tsv"
  if not shutdown_path.exists():
    return
  try:
    shutdown_path.unlink()
  except OSError as error:
    logging.error(
      f'Error: Failed to remove stale shutdown request file {str(shutdown_path)!r}: {error}'
    )


def show_shutdown_alert(remaining_seconds: int, *, log: bool, gui: bool):
  """Send shutdown alerts based on remaining time.
  `remaining_seconds` is the remaining delay until shutdown.
  `log` controls logging the constructed message.
  `gui` controls sending desktop notifications. """

  now = datetime.datetime.now()

  if remaining_seconds > 0:
    minutes = (remaining_seconds + 59)//60
    shutdown_time = now + datetime.timedelta(seconds=remaining_seconds)
    shutdown_time_str = shutdown_time.strftime('%I:%M %p').lstrip('0')
    message = (
      f'System will shut down at {shutdown_time_str} '
      f'(in {minutes} minute' + ('s' if minutes != 1 else '') + ').'
    )
  else:
    message = 'System is shutting down now.'

  if log:
    logging.info(message)

  if not gui:
    return

  # Try to send a desktop notification via notify-send.
  cmd: tuple[str,...] = ('notify-send', 'Shutdown requested', message)
  try:
    subprocess.run(cmd, check=False)
  except (FileNotFoundError, OSError) as error:
    logging.error(f'Error: notify-send failed: {error}')

  # Also try to show a warning dialog via zenity.
  cmd = ('zenity', '--warning', '--title', 'Shutdown requested', '--text', message)
  try:
    subprocess.Popen(
      cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
      start_new_session=True
    )
  except (FileNotFoundError, OSError) as error:
    logging.error(f'Error: zenity failed: {error}')


def parse_time(min_str: str) -> int:
  """Parse a delay string as minutes.
  Accepts either "MM" (minutes) or "HH:MM" (hours and minutes).
  Returns the delay in seconds.
  Raises ValueError on parse failure.
  """
  if ':' in min_str:
    parts = min_str.split(':', 1)
    if len(parts) != 2:
      raise ValueError(f'Invalid time string (expected HH:MM): {min_str!r}')
    hours_str, minutes_str = parts
    try:
      hours = int(hours_str)
    except ValueError as error:
      raise ValueError(f'Invalid hour value in time string: {min_str!r}') from error
    try:
      minutes = int(minutes_str)
    except ValueError as error:
      raise ValueError(f'Invalid minute value in time string: {min_str!r}') from error
    if hours < 0 or minutes < 0:
      raise ValueError(f'Negative time not allowed: {min_str!r}')
    total_minutes = hours * 60 + minutes
  else:
    try:
      total_minutes = int(min_str)
    except ValueError as error:
      raise ValueError(f'Invalid minute value in time string: {min_str!r}') from error
    if total_minutes < 0:
      raise ValueError(f'Negative time not allowed: {min_str!r}')
  return total_minutes * 60


def shutdown():
  # pydbus is required: `sudo apt install python3-pydbus` on Ubuntu 24.04 (also installs gi)
  # This is the only way (I know of) to shut down without sudo.
  import gi
  import pydbus
  # https://stackoverflow.com/questions/23013274/shutting-down-computer-linux-using-python/23013969#23013969
  bus = pydbus.SystemBus()
  try:
    proxy = bus.get('org.freedesktop.login1', '/org/freedesktop/login1')
  except (ValueError, gi.repository.GLib.GError) as error:
    logging.error(f'Error: Failed to find the org.freedesktop.login1 service: {error}')
    return False
  logging.info('Shutting down.')
  if proxy.CanPowerOff():
    proxy.PowerOff(False)
  else:
    logging.error('Error: System does not support shutting down.')
    return False


COMMANDS = {
  'echo':{
    'execute': do_echo,
  },
  'cat':{
    'execute': do_cat,
  },
  'shutdown':{
    'execute': do_shutdown,
    'on_absent': on_absent_shutdown,
  },
}


def in_whitelist(path, whitelist):
  for directory in whitelist:
    if str(path).startswith(f'{directory}/'):
      return True
  return False


def fail(message):
  logging.critical('Error: '+str(message))
  if __name__ == '__main__':
    sys.exit(1)
  else:
    raise Exception(message)


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except BrokenPipeError:
    pass
