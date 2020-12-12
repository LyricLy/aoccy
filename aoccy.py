import functools
import itertools
import inspect
import re
import resource
import sys
from contextlib import contextmanager


_, hard = resource.getrlimit(resource.RLIMIT_STACK)
if hard == resource.RLIM_INFINITY:
    # our hard limit is infinity, which means that the OS won't object if we, uh, ask for a stack of infinite size
    resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))
    sys.setrecursionlimit(2**31-1)


class View:
    def __init__(self, source):
        self.source = source
        self.idx = 0
        self.line = 0
        self.column = 0

    def save(self):
        return self.idx, self.line, self.column

    def load(self, data):
        self.idx, self.line, self.column = data

    def consume(self, n):
        t = self[:n]
        self.idx += n
        self.line += t.count("\n")
        if "\n" in t:
            self.column = len(t.rsplit("\n", 1)[1])
        else:
            self.column += len(t)
        return t

    def __getitem__(self, i):
        if isinstance(i, slice):
            start = i.start + self.idx if i.start is not None else self.idx
            stop = i.stop and i.stop + self.idx
            return self.source[start:stop:i.step]
        else:
            return self.source[i + self.idx]


class ParseResult:
    __slots__ = ("result", "succeeded", "consumed", "expected")

    def __init__(self, result=None, *, success, consumed=True, expected=None):
        self.result = result
        self.succeeded = success
        self.consumed = consumed
        self.expected = expected or set()


def english_format_list(strings):
    strings = list(strings)
    if len(strings) == 1:
        return strings[0]
    return ", ".join(strings[:-1]) + ","*(len(strings)>2) + " or " + strings[-1]

def format_error(view, expected):
    o = []
    expected_len = 1
    line = view[-view.column:].split("\n", 1)[0]

    o.append(f"{view.line+1}:{view.column+1}:")
    t = " "*len(str(view.line+1)) + " | "
    o.append(t)
    o.append(f"{view.line+1} | {line}")
    o.append(t + " "*view.column + "^"*expected_len)
    if expected:
        unexpected = view[:expected_len]
        o.append(f"unexpected {unexpected!r}" if unexpected else "unexpected EOF")
        o.append(f"expected {english_format_list(expected)}")
    else:
        o.append("Parsing failed (no information)")
    return "\n".join(o)

class ParseError(Exception):
    def __str__(self):
        return "Details below.\n" + self.args[0]

    # INCREDIBLY evil
    def __del__(self):
        sys.excepthook = sys.__excepthook__


class Parser:
    def __init__(self, logic):
        self.logic = logic

    def parse(self, view):
        return self.logic(view=view)

    def __getitem__(self, i):
        if isinstance(i, slice):
            low, high = i.start or 0, i.stop
        else:
            low = high = i
        return _between(self, low, high)

    def __invert__(self):
        return optional(self)

    def __or__(self, other):
        return _or(self, other)

    def __truediv__(self, other):
        return _or(self, other)

    def __xor__(self, other):
        return _or(cp(self), other)

    def __rshift__(self, other):
        return _then(self, other).map(lambda l: l[1])

    def __lshift__(self, other):
        return _then(self, other).map(lambda l: l[0])

    def __and__(self, other):
        return _then(self, other)

    def __add__(self, other):
        return _then(self, other)

    def __call__(self, func):
        return _bind(self, func)

    def label(self, l):
        return _label(self, l)

    def map(self, func):
        return _map(self, func)

    def set(self, val):
        return _map(self, lambda _: val)

    def parse_text(self, text):
        view = View(text)
        res = self.parse(view)
        if res.succeeded:
            return res.result
        else:
            def except_handler(t, ex, tb):
                sys.excepthook = sys.__excepthook__
                print(ex.args[0], file=sys.stderr)
            sys.excepthook = except_handler
            raise ParseError(format_error(view, res.expected)) from None


def parser(func):
    def inner(*args, **kwargs):
        f = functools.partial(func, *args, **kwargs)
        return Parser(f)
    inner.__name__ = func.__name__
    return inner


@parser
def _map(p, func, *, view):
    t = p.parse(view)
    if t.succeeded:
        t.result = func(t.result)
    return t

@parser
def _label(p, label, *, view):
    t = p.parse(view)
    if not t.consumed and t.expected:
        t.expected = {label}
    return t

def _maybe_parse(p, view, last=None):
    t = and_then(view, last, p)
    if not t.succeeded:
        if not t.consumed:
            t.succeeded = True
        return False, t
    return True, t

@parser
def _between(p, low, high, *, view):
    results = []

    for _ in range(low):
        # we *must* succeed
        t = p.parse(view)
        if not t.succeeded:
            return t
        results.append(t.result)

    for _ in range(high - low) if high is not None else itertools.count():
        # we *may* succeed
        res = p.parse(view)
        if not res.succeeded:
            if res.consumed:
                return res
            break
        results.append(res.result)
    return ParseResult(results, success=True, consumed=bool(high), expected=res.expected)

@parser
def optional(p, *, view):
    t = p.parse(view)
    if not t.succeeded:
        if t.consumed:
            return t
        t.succeeded = True
    return t

@parser
def lookahead(p, *, view):
    now = view.save()
    t = p.parse(view)
    view.load(now)
    t.consumed = False
    return t

@parser
def cp(p, *, view):
    now = view.save()
    t = p.parse(view)
    if not t.succeeded and t.consumed:
        view.load(now)
        t.consumed = False
    return t

@parser
def defer(f, *, view):
    return f().parse(view)

@parser
def _or(first, second, *, view):
    t1 = first.parse(view)
    if t1.succeeded or t1.consumed:
        return t1
    t2 = second.parse(view)
    if t2.succeeded or t2.consumed:
        return t2

    return ParseResult(success=False, consumed=False, expected=t1.expected|t2.expected)

@parser
def _then(first, second, *, view):
    t = first.parse(view)
    if not t.succeeded:
        return t
    t2 = second.parse(view)
    t2.consumed = t2.consumed or t.consumed
    if not t2.succeeded or t2.expected:
        t2.expected |= t.expected
    t2.result = t.result, t2.result
    return t2

@parser
def lit(string, *, view):
    t = view[:len(string)]
    if t == string:
        return ParseResult(view.consume(len(string)), success=True)
    else:
        return ParseResult(success=False, consumed=False, expected={repr(string)})

@parser
def regex(pattern, *, view):
    m = re.match(pattern, view[:])
    if m is None:
        return ParseResult(success=False, consumed=False, expected={f"text matching {repr(pattern)}"})
    view.consume(m.end())
    return ParseResult(m, success=True)

@parser
def _eof(*, view):
    if view.idx == len(view.source):
        return ParseResult(success=True, consumed=False)
    return ParseResult(success=False, consumed=False, expected={"EOF"})
eof = _eof()

@parser
def _pos(*, view):
    return ParseResult((view.line, view.column), success=True, consumed=False)
pos = _pos()

@parser
def pure(v):
    return ParseResult(v, success=True, consumed=False)

@parser
def _empty():
    return ParseResult(success=False, consumed=False)
empty = _empty()

def lexeme_gen(ws):
    def lexeme(p):
        return p << ws
    return lexeme

def symbol_gen(ws):
    def symbol(s):
        return lit(s) << ws
    return symbol

def sep_by(sep, p):
    return (~(p & (sep >> p)[:])).map(lambda p: [p[0]] + p[1] if p else [])

def sep_end_by(sep, p):
    return sep_by(sep, p) << ~sep
