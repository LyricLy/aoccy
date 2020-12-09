import functools
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


class ParseError(Exception):
    def __init__(self, *, consumed, expected, expected_len=1, view=None):
        self.consumed = consumed
        self.pected = expected
        self.pected_len = expected_len
        self.view = view

    def __str__(self):
        return "Details below.\n" + self.format()

    def format(ex):
        view = ex.view
        o = []
        line = view[-view.column:].split("\n", 1)[0]
        o.append(f"{view.line+1}:{view.column+1}:")
        t = " "*len(str(view.line+1)) + " | "
        o.append(t)
        o.append(f"{view.line+1} | {line}")
        o.append(t + " "*view.column + "^"*ex.pected_len)
        if ex.pected_len:
            unexpected = view[:ex.pected_len]
            if unexpected:
                o.append(f"unexpected {unexpected!r}")
            else:
                o.append("unexpected EOF")
        expected = ex.pected | view.hints
        o.append(f"expected {english_format_list(expected)}")
        return "\n".join(o)

    # INCREDIBLY evil
    def __del__(self):
        if self.view is not None:
            sys.excepthook = sys.__excepthook__


class View:
    def __init__(self, source):
        self.source = source
        self.idx = 0
        self.line = 0
        self.column = 0
        self.hints = set()

    def save(self):
        return self.idx, self.line, self.column, self.hints.copy()

    def load(self, data):
        self.idx, self.line, self.column, self.hints = data

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

def english_format_list(strings):
    strings = list(strings)
    if len(strings) == 1:
        return strings[0]
    return ", ".join(strings[:-1]) + ","*(len(strings)>2) + " or " + strings[-1]

class Parser:
    def __init__(self, logic):
        self.logic = logic

    def parse(self, view):
        l = len(view.hints)
        r = self.logic(view=view)
        if len(view.hints) <= l:
            view.hints.clear()
        return r

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
        return _then(self, other).bind(lambda l: l[1])

    def __lshift__(self, other):
        return _then(self, other).bind(lambda l: l[0])

    def __and__(self, other):
        return _then(self, other)

    def __add__(self, other):
        return _then(self, other)

    def __call__(self, func):
        return _bind(self, func)

    def label(self, l):
        return _label(self, l)

    def bind(self, func, *, map=False):
        return _bind(self, func, map=map)

    def set(self, val):
        return _bind(self, lambda _: val)

    def parse_text(self, text):
        view = View(text)
        try:
            return self.parse(view)
        except ParseError as ex:
            ex.view = view
            def except_handler(t, ex, tb):
                sys.excepthook = sys.__excepthook__
                print(ex.format(), file=sys.stderr)
            sys.excepthook = except_handler
            raise ex


def parser(func):
    def inner(*args, **kwargs):
        f = functools.partial(func, *args, **kwargs)
        return Parser(f)
    inner.__name__ = func.__name__
    return inner


@parser
def _bind(p, func, map=False, *, view):
    t = p.parse(view)

    res = func(t)

    if not isinstance(res, Parser) or self.map:
        # treat like <$>
        return res
    else:
        # treat like >>=
        return res.parse(view)

@parser
def _label(p, label, *, view):
    try:
        return p.parse(view)
    except ParseError as ex:
        if ex.consumed:
            raise
        ex.pected = {label}
        raise ex

def _maybe_parse(p, view):
    try:
        return True, p.parse(view)
    except ParseError as ex:
        if ex.consumed:
            raise
        view.hints |= ex.pected
        return False, None

@parser
def _between(p, low, high, *, view):
    results = []
    high = high or float("inf")
    for _ in range(low):
        # we *must* succeed
        results.append(p.parse(view))
    count = low
    while count < high:
        count += 1
        # we *may* succeed
        success, res = _maybe_parse(p, view)
        if not success:
            break
        results.append(res)
    return results

@parser
def optional(p, *, view):
    return _maybe_parse(p, view)[1]

@parser
def lookahead(p, *, view):
    now = view.save()
    try:
        return p.parse(view)
    except ParseError as ex:
        ex.consumed = False
        raise ex
    finally:
        view.load(now)

@parser
def cp(p, *, view):
    now = view.save()
    try:
        return p.parse(view)
    except ParseError as ex:
        if ex.consumed:
            view.load(now)
            ex.consumed = False
        raise ex

@parser
def defer(f, *, view):
    return f().parse(view)

@parser
def _or(first, second, *, view):
    try:
        return first.parse(view)
    except ParseError as ex:
        if ex.consumed:
            raise
        try:
            return second.parse(view)
        except ParseError as ex2:
            if ex2.consumed:
                raise
            raise ParseError(consumed=False, expected=ex.pected|ex2.pected, expected_len=min(ex.pected_len, ex2.pected_len))

@parser
def _then(first, second, *, view):
    f = first.parse(view)
    try:
        s = second.parse(view)
    except ParseError as ex:
        ex.consumed = True
        raise ex
    return f, s

@parser
def lit(string, *, view):
    t = view[:len(string)]
    if t == string:
        return view.consume(len(string))
    else:
        raise ParseError(consumed=False, expected={repr(string)}, expected_len=len(string))

@parser
def regex(pattern, *, view):
    m = re.match(pattern, view[:])
    if m is None:
        raise ParseError(consumed=False, expected={f"text matching {repr(pattern)}"})
    view.consume(m.end())
    return m

@parser
def _eof(*, view):
    if view.idx == len(view.source):
        return None
    raise ParseError(consumed=False, expected={"EOF"})
eof = _eof()

@parser
def _pos(*, view):
    return (view.line, view.column)
pos = _pos()

def lexeme_gen(ws):
    def lexeme(p):
        return p << ws
    return lexeme

def symbol_gen(ws):
    def symbol(s):
        return lit(s) << ws
    return symbol

def sep_by(sep, p):
    return (~(p & (sep >> p)[:])).bind(lambda p: [p[0]] + p[1] if p else [])

def sep_end_by(sep, p):
    return sep_by(sep, p) << ~sep
