#!/usr/bin/env python
from __future__ import division
import os
import sys
import math
import string
import subprocess
import distutils.spawn
from optparse import OptionParser

COLUMNS_DEFAULT = 80
# target ratio for width of field1 / width of field6
RATIO = 1.5
MIN_FIELD1 = 10
ALIGN = ['-', '', '', '', '', '-']

OPT_DEFAULTS = {'str':'', 'int':0, 'float':0.0, 'debug':False}
USAGE = "USAGE: %prog"
DESCRIPTION = ("""Run df -h, and if the output of df -h is wider than the
current terminal, shrink the first and last columns to fit. Here is the specific
algorithm: If the df -h output already fits in the terminal, print it. If it
doesn't, try the following steps, until it fits: First, shrink the whitespace
down to one space between each field. Then, if the ratio of the widths of the
first and last columns isn't """+str(RATIO)+""", shrink one of them until either
it fits in the terminal or the ratio is """+str(RATIO)+""". Then continue
shrinking them in the same ratio until it fits.""")
EPILOG = """"""

def main():

  parser = OptionParser(usage=USAGE, description=DESCRIPTION, epilog=EPILOG)

  parser.add_option('-s', '--str', dest='str',
    default=OPT_DEFAULTS.get('str'), help='default: %default')
  parser.add_option('-i', '--int', dest='int', type='int',
    default=OPT_DEFAULTS.get('int'), help='')
  parser.add_option('-f', '--float', dest='float', type='float',
    default=OPT_DEFAULTS.get('float'), help='')
  parser.add_option('-d', '--debug', dest='debug', action='store_true',
    default=OPT_DEFAULTS.get('debug'),
    help='Turn on debug mode.')

  (options, arguments) = parser.parse_args()

  devnull = open(os.devnull, 'wb')
  try:
    dfoutput = subprocess.check_output(['df', '-h'], stderr=devnull)
  except OSError:
    devnull.close()
    fail("Error running df -h")
  devnull.close()

  term_width = get_columns(COLUMNS_DEFAULT)
  output_width = get_output_width(dfoutput)

  # if it fits already, print and exit
  diff = output_width - term_width
  if diff <= 0:
    sys.stdout.write(dfoutput)
    sys.exit(0)
  
  dflines = dfoutput.splitlines()
  (all_starts, all_widths) = get_starts_and_widths(dflines)

  starts = minmax_stats(all_starts, min)
  widths = minmax_stats(all_widths, max)

  # from here on, it's specific to df -h output
  validate_starts(all_starts)
  # get rid of 7th column that exists only because of the "on" in "Mounted on"
  starts.pop()
  widths.pop()

  # Print if it already fits with shrunk whitespace
  print_if_fits(term_width, widths, dflines, align=ALIGN)

  # Is the 1st/6th ratio too high?
  ideal_width1 = int(math.ceil(RATIO * widths[-1]))
  ideal_width6 = int(math.ceil(widths[0] / RATIO))
  field1_diff = widths[0] - ideal_width1
  field6_diff = widths[-1] - ideal_width6
  term_diff = sum(widths) + len(widths) - 1 - term_width
  if options.debug:
    print "ideal_width1 / actual_width6 =",ideal_width1,'/',widths[-1],'=',(ideal_width1/widths[-1])
    print "actual_width1 / ideal_width6 =",widths[0],'/',ideal_width6,'=',(widths[0]/ideal_width6)
  if field1_diff > 0:
    if options.debug:
      print "reducing width1 by",min(term_diff, field1_diff)
    widths[0] = widths[0] - min(term_diff, field1_diff)
  elif field6_diff > 0:
    if options.debug:
      print "reducing width6 by",min(term_diff, field6_diff)
    widths[-1] = widths[-1] - min(term_diff, field6_diff)
  print_if_fits(term_width, widths, dflines, align=ALIGN)
  
  # Shrink until it fits or the minimum width is reached
  term_diff = sum(widths) + len(widths) - 1 - term_width
  # divide term_diff into two parts proportional to RATIO
  field1_diff = int(round(term_diff*RATIO/(1+RATIO)))
  field6_diff = term_diff - field1_diff
  if options.debug:
    print "reducing width1 by",field1_diff,"from",widths[0]
    print "reducing width6 by",field6_diff,"from",widths[-1]
  widths[0] = widths[0] - field1_diff
  widths[-1] = widths[-1] - field6_diff
  if widths[0] < MIN_FIELD1:
    widths[0] = MIN_FIELD1
    widths[-1] = int(round(widths[0] / RATIO))
  # print even if it doesn't fit: term_width = 1000
  print_if_fits(1000, widths, dflines, align=ALIGN)



def get_columns(default=None):
  """Get current terminal width, using stty command. If stty isn't available,
  or if it gives an error, return the default. Note: requires Python 2.7"""
  if not distutils.spawn.find_executable('stty'):
    return default
  devnull = open(os.devnull, 'wb')
  try:
    output = subprocess.check_output(['stty', 'size'], stderr=devnull)
  except (OSError, subprocess.CalledProcessError):
    return default
  finally:
    devnull.close()
  return int(output.split()[1])


def get_output_width(output):
  """Get the width of the longest line in the output."""
  max_width = 0
  for line in output.split('\n'):
    max_width = max(max_width, len(line))
  return max_width


def get_starts_and_widths(lines):
  """Get the coordinates of the starts of fields, plus their widths.
  Each start is the character coordinate of where the space-delimited field
  starts. Each width is the length of the non-whitespace string composing the
  field. Returns a list of starts, one per line. Each start is a list of int's,
  one per field.
  """
  all_starts = []
  all_widths = []
  for line in lines:
    if not line.strip():
      continue
    in_whitespace = True
    starts = []
    widths = []
    line_width = 0
    for (i, char) in enumerate(line):
      if char in string.whitespace:
        if not in_whitespace:
          widths.append(i - starts[-1])
        in_whitespace = True
      else:
        if in_whitespace:
          starts.append(i)
        in_whitespace = False
      line_width = i+1
    if not in_whitespace:
      widths.append(line_width - starts[-1])
    all_starts.append(starts)
    all_widths.append(widths)
  return (all_starts, all_widths)


def validate_starts(all_starts):
  """Make sure they're consistent with the expected df -h output."""
  for (line_num, line) in enumerate(all_starts):
    if line_num == 0:
      if len(line) != 7:
        fail("Error: Unexpected df -h output. Wrong number of whitespace-"
          +"delimited header columns (saw "+str(len(line))+" columns).")
    else:
      if len(line) != 6:
        fail("Error: Unexpected df -h output. Wrong number of whitespace-"
          +"delimited columns on line "+str(line_num)+" (saw "+str(len(line))
          +" columns).")


def minmax_stats(all_stats, func):
  """Get the min or max value for each column in the output.
  First argument is a list of lines, each line being a list of stats for each
  field. Second argument is the function to use for the comparison. Must take
  two arguments and return one of them, like min and max."""
  stats = []
  for line in all_stats:
    for (field, field_start) in enumerate(line):
      if len(stats) <= field:
        stats.append(field_start)
      else:
        stats[field] = func(stats[field], field_start)
  return stats


def print_if_fits(term_width, widths, lines, align=None):
  """Print and exit if the output would fit in the terminal."""
  # does it fit?
  current_width = sum(widths) + len(widths) - 1
  diff = current_width - term_width
  if diff > 0:
    return
  # print by joining fields truncated to given widths
  if not align:
    align = ['-'] * len(widths)
  for line in lines:
    if not line:
      continue
    out_fields = []
    for (i, field) in enumerate(line.split()):
      if i >= len(widths):
        continue
      format = '%'+align[i]+str(widths[i])+'s'
      out_fields.append(format % field[:widths[i]])
    print ' '.join(out_fields)
  sys.exit(0)


def fail(message):
  sys.stderr.write(message+"\n")
  sys.exit(1)

if __name__ == "__main__":
  main()
