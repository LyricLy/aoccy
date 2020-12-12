# A compliant JSON parser.


import sys
from ast import literal_eval

from aoccy import *


ws = regex(r"[ \n\r\t]*").label("whitespace")
lexeme = lexeme_gen(ws)
sym = symbol_gen(ws)

def comma_sep(p):
    return sep_by(sym(","), p)

number = lexeme(regex(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?").label("a number").map(lambda m: float(m.group(0))))
string = lexeme(regex(r'"(?:[^"\\\x00-\x1F]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"').label("a string").map(lambda m: literal_eval(m.group(0))))
singleton = sym("true").set(True) | sym("false").set(False) | sym("null").set(None)
array = defer(lambda: sym("[") >> comma_sep(value) << sym("]")).label("an array")
object = defer(lambda: sym("{") >> comma_sep(string & sym(":") >> value).map(dict) << sym("}")).label("an object")
value = (number | string | singleton | array | object).label("a value")
json = ws >> value << eof

if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(json.parse_text(f.read()))
