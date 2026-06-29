"""Colored terminal output for the voice assistant."""

import json
import sys


class TerminalUI:
  RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
  CYAN, GREEN, YELLOW, MAGENTA, RED = "\033[36m", "\033[32m", "\033[33m", "\033[35m", "\033[31m"

  def __init__(self, debug=False):
    self.debug_enabled = debug
    self.use_color = sys.stdout.isatty()
    self.turn = 0

  def _paint(self, color, text, bold=False):
    if not self.use_color:
      return text
    return f"{self.BOLD if bold else ''}{color}{text}{self.RESET}"

  def divider(self, label=None):
    line = "─" * 44
    print(self._paint(self.DIM, f"\n{line}  {label}  {line}" if label else f"\n{line}"))

  def startup(self, msg):
    print(self._paint(self.DIM, msg))

  def ready(self, levels=None):
    if levels and self.debug_enabled:
      print(self._paint(self.GREEN, f"[Ready] {levels}"))
    print(self._paint(self.GREEN, "[Ready] speak (Ctrl+C to quit).\n"))

  def user_speaking(self):
    self.turn += 1
    self.divider(f"Turn {self.turn}")
    print(self._paint(self.CYAN, "\n[You] speaking...", bold=True))

  def user_finished(self):
    print(self._paint(self.CYAN, "[You] finished."))

  def user_heard(self, text, note=None):
    line = f'[Heard you] "{text}"' + (f" ({note})" if note else "")
    print(self._paint(self.CYAN, line))

  def assistant_responding(self):
    print(self._paint(self.MAGENTA, "\n[Assistant] responding...", bold=True))

  def assistant_said(self, text):
    print(self._paint(self.MAGENTA, f'[Assistant] "{text}"', bold=True))

  def assistant_cancelled(self):
    print(self._paint(self.YELLOW, "[Assistant] response cancelled."))

  def echo_block(self, reason):
    print(self._paint(self.YELLOW, f"[Echo] blocked ({reason}) — waiting for room to quiet..."))

  def echo_cancel(self):
    print(self._paint(self.YELLOW, "[Echo] cancelling phantom response"))

  def waiting(self, played_sec, min_wait):
    print(self._paint(self.DIM, f"[Waiting] room quiet (played {played_sec:.1f}s, min wait {min_wait:.1f}s)..."))

  def calibrate_quiet(self):
    print(self._paint(self.DIM, "[Calibrate] stay quiet for a moment..."))

  def calibrate_speak(self):
    print(self._paint(self.CYAN, "[Calibrate] now say a few words (e.g. \"hello\")...", bold=True))

  def calibrate_done(self, noise, speech_peak):
    print(self._paint(self.GREEN, f"[Calibrate] done — noise={noise:.0f}, your voice≈{speech_peak:.0f}"))

  def warn(self, msg):
    print(self._paint(self.YELLOW, f"[Warning] {msg}"))

  def debug(self, msg):
    if self.debug_enabled:
      print(self._paint(self.DIM, f"[Debug] {msg}"))

  def server_event(self, event_type):
    if self.debug_enabled:
      print(self._paint(self.DIM, f"  <- {event_type}"))

  def api_notice(self, message):
    print(self._paint(self.DIM, f"[API] {message}"))

  def error(self, event):
    print(self._paint(self.RED, "ERROR: " + json.dumps(event, indent=2)))
