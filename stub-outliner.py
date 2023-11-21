
from typing import (Any, Collection, Sequence, Optional, TextIO, overload, TypeVar, Union)
from types import (FunctionType, LambdaType, GeneratorType, WrapperDescriptorType,
                   MethodDescriptorType, ClassMethodDescriptorType)
import inspect
from inspect import formatannotation
from dataclasses import dataclass
import ast
import io

import wx
import pyparsing as ps


# Parsing:
# =======

ps.ParserElement.enablePackrat()
ident = ps.pyparsing_common.identifier
val = ident | ps.QuotedString('"') | ps.pyparsing_common.number | "[]" | "()"
arg = ps.Combine(ident + ps.Optional("=" + val), adjacent=False)
args = (ps.Suppress('(')
        + ps.Optional(ps.delimitedList(arg, delim=','))
        + ps.Suppress(")")).setResultsName("args")
name = ident("name")
# wx docstrings sometimes have a tuple as the return anotation
tp_repr = ps.Combine("(" + ident + ps.ZeroOrMore("," + ident) + ")",
                     adjacent=False)
ret = ps.Optional(ps.Suppress("->") + (tp_repr|ident),
                  default="None")("return")
func = name + args + ret
del ident, val, arg, args, name, tp_repr, ret


# Type Heuristics:
# ===============

type_to_arg = {
    # don't include "w*" becuse that might be a window:
    "int": ["x*", "y*", "h*", "width", "height"], 
    # "id" is not neceseraly a window id. The "id" parameter of Window and
    # Event __init__ methods is handled specialy in the stub generation code:
    "wx._WindowID": ["windid", "winid"],
    "PointLike": ["pt*", "point"],
    "SizeLike": ["dim", "sz", "size"],
    "RectLike": ["rect"],
    "wx.Bitmap": ["bitmap*", "bmp*", "*Bitmap"],
    "wx.Window": ["window"],
    "wx._EventType": ["*eventType", "evtType"],
    "wx.DC": ["dc"],
    "String": ["label", "text", "str", "string"],
    "Any": ["clientData"],
    "wx.Icon": ["icon"],
    "wx.Colour": ["*Colour", "colour"],
    "wx.Font": ["font"],
    "wx.ImageList": ["imageList"],
}

patern_and_type = [(p, t) for t, v in type_to_arg.items() for p in v]
patern_and_type.sort(key=len, reverse=True) # catch longest match first

def match(string: str, patern: str) -> bool:
    stem = patern.strip("*")
    return(
        string == stem
        ) or (
        patern.startswith("*")
        and string.endswith(stem)
        ) or (
        patern.endswith("*")
        and string.startswith(stem)
        and string[len(stem)].isupper()
        )

# do this by name not by actual type as it is used in property inferance
convert_arg_type = {
    "str": "String",
    "bytes": "String",
    "list": "Sequence[]",
    "StandardID": "wx._WindowID",
    "Point": "PointLike",
    "Size": "SizeLike",
    "Rect": "RectLike",
}

def get_type_from_default(string: str) -> Optional[str]:
    x: object
    if string in ("True", "False"):
        x = True
    elif string.isidentifier():
        x = getattr(wx, string, None)
    else:
        try:
            x = ast.literal_eval(string)
        except Exception:
            x = None
    if x is None:
        return None
    tp: type = type(x)
    name = tp.__name__
    if name in convert_arg_type:
        return convert_arg_type[name]
    if tp.__module__.startswith("wx"):
        return "wx." + name
    return name

ret_trans_table = {
    "PyUserData": "Any",
    "ClientData": "Any",
    "PyObject": "?",
    "String": "str",
    "double": "float",
    "long": "int",
    "LongLong": "int",
    "unsignedchar": "int",
    "unsignedint": "int",
    "Uint32": "int",
    "UIntPtr": "int",
    "Coord": "int",
    "size_t": "int",
    "IntPtr": "int",
    "Uint16": "int",
    "unsignedlong": "int",
    "ArrayString": "list[str]",
    "ArrayInt": "list[int]",
}

def modify_ret(ret: str) -> str:
    if ret in ret_trans_table:
        return ret_trans_table[ret]
    if hasattr(wx, ret):
        return "wx." + ret
    return ret


# Signatures:
# ==========

@dataclass
class Arg:
    name: str
    tp: Optional[str]
    deafult: bool

def parse_args(args: Sequence[str], add_any: bool = False) -> list[Arg]:
    r: list[Arg] = []
    for arg in args:
        name, _, default = arg.partition("=")
        tp = None
        if default:
            tp = get_type_from_default(default)
        else:
            for a, t in patern_and_type:
                if match(a, name):
                    tp = t
                    break
        if tp is None and add_any:
            tp = "Any"
        r.append(Arg(name, tp, bool(default)))
    return r

_null = object()
T = TypeVar("T")

class Signature:
    def __init__(self, parsed: Any, init: bool = False, meth: bool = False,
                 add_any: bool = False) -> None:
        if init:
            meth = True
        self.retanot: str = modify_ret(parsed["return"][0])
        self.name: str = parsed["name"] if not init else "__init__"
        args = [] if not meth else [Arg("self", None, False)]
        args.extend(parse_args(parsed["args"], add_any=add_any))
        self.args: list[Arg] = args

    @overload
    def index(self, name: str) -> int: ...
    
    @overload
    def index(self, name: str, default: T) -> Union[int, T]: ...
    
    def index(self, name: str, default: Any = _null) -> Any:
        for i, a in enumerate(self.args):
            if a.name == name:
                return i
        if default is not _null:
            return default
        raise IndexError(name)
    
    def set_type(self, i: int, tp: str) -> None:
        self.args[i].tp = tp
    
    def print(self, indent="    ", file=TextIO) -> None:
        args: list[str] = []
        append = args.append
        for arg in self.args:
            append(arg.name)
            if arg.tp is not None:
                append(": ")
                append(arg.tp)
            if arg.deafult:
                append(" = ...")
            append(", ")
        if args:  # remove trailing comma
            args.pop()
        print(indent, "def ", self.name, "(", "".join(args), ") -> ",
              self.retanot, ": ...", sep="", file=file)

def signature_using_inspect(func: Any, add_any: bool = False) -> str:
    try:
        sig = inspect.signature(func)
    except ValueError:
        if add_any:
            return "(self, /, *args: Any, **kwargs: Any)"
        else:
            return "(self, /, *args, **kwargs)"
    else:
        if add_any:
            parameters = [p.replace(annotation=Any) for p in sig.parameters.values()]
            if parameters[0].name == "self":
                parameters[0] = parameters[0].replace(annotation=inspect.Parameter.empty)
            sig = sig.replace(parameters=parameters)
        return str(sig)

deafult_signatures = {
    signature_using_inspect(object.__init__),
    signature_using_inspect(object.__init__, True),
    # 'wx.Abort' will raise ValueError when passed to inspect.signature
    signature_using_inspect(wx.Abort),
    signature_using_inspect(wx.Abort, True)
    }

def find_signatures(doc: str, name: str, **kwargs) -> list[Signature]:
    r = []
    for parsed in func.scanString(doc):
        parsed = parsed[0]
        if parsed["name"] == name:
            r.append(Signature(parsed, **kwargs))
    return r


# Stub File:
# =========

# sip.methoddecriptor, not the actual method returned by __getattr__ look up:
sip_method_descripter: type = type(wx.Object.__dict__["Destroy"])

# sip.variabledescriptor:
#XXXsip_variable_descriptor: type = type(wx.VideoMode.__dict__["h"])

# sip.wraper, base class of all sip objects:
sip_object: type = wx.Trackable.__bases__[0]

# sip.enumtype, a virtual subclass of int:
sip_enum: type = type(wx.Direction)

# Types ocuring in a class's __dict__ that represent methods.
method_descripters = {
    FunctionType, LambdaType, GeneratorType, WrapperDescriptorType,
    MethodDescriptorType, ClassMethodDescriptorType, classmethod,
    staticmethod, sip_method_descripter,
    }

# This is not actualy used, I'm not sure what I wanted it for.
special_names = {'__add__', '__divmod__', '__floordiv__', '__ge__', '__gt__',
                '__le__', '__lt__', '__mod__', '__mul__', '__pow__', '__radd__',
                '__rdivmod__', '__rfloordiv__', '__rmod__', '__rmul__', '__rpow__',
                '__rsub__', '__rtruediv__', '__sub__', '__truediv__'}


def make_stub(cls: type, file: TextIO, add_any: bool = False, all_methods: bool = False,
              hide_mod: Collection[type] = ()) -> None:
    objects_by_category: dict[str, list[str]] = {
        "method": [], "property": [], "other": [], "enumclass": []
        }
    cls_dict = cls.__dict__
    ignore = {"__module__", "__weakref__", "__iadd__", "__isub__", "__dict__",
              "__bool__", "__nonzero__"}
    if not all_methods:
        for supercls in cls.mro()[1:]:
            # if method is defined in a superclass assume same signature.
            ignore.update(supercls.__dict__)
    else:
        ignore.update(object.__dict__)
        ignore.difference_update(('__ge__', '__gt__', '__lt__', '__le__'))
    for name, obj in cls_dict.items():
        if name in ignore:
            pass
        elif type(obj) in method_descripters:
            objects_by_category["method"].append(name)
        elif type(obj) is property:
            objects_by_category["property"].append(name)
        elif type(obj) is sip_enum:
            objects_by_category["enumclass"].append(name)
        elif (name[0] != "_") or (name.startswith("__") and name.endswith("__")):
            objects_by_category["other"].append(name)
    print("class ", cls.__name__, "(", sep="", end="", file=file)
    bases = []
    for base in cls.__bases__:
        if base in (object, sip_object):
            pass
        elif base in hide_mod:
            bases.append(base.__name__)
        elif base.__module__.startswith("wx"):
            bases.append("wx." + base.__name__)
        else:
            bases.append(base.__module__ + "." + base.__name__)
    print(*bases, sep=", ", end="):\n", file=file)

    # First do __init__; get the signatures from class docstring.
    doc = cls.__doc__ or " "
    sigs = find_signatures(doc, cls.__name__, init=True, add_any=add_any, meth=True)
    if not sigs:
        # sip classes don't have a regular __init__ method,
        # but this is useful for pure-python classes.
        doc = cls.__init__.__doc__ or " "  # type: ignore  # direct acsses of __init__
        sigs = find_signatures(doc, cls.__name__, init=True, add_any=add_any, meth=True)
    if sigs:
        # __init__ arguments have special heuristcs:
        for sig in sigs:
            # args[0] is 'self' argument
            if len(sig.args) < 2:
                pass
            elif issubclass(cls, wx.Window) and sig.args[1].name == "parent":
                if issubclass(cls, wx.TopLevelWindow):
                    sig.set_type(1, "Optional[wx.Window]")
                else:
                    sig.set_type(1, "wx.Window")
                if sig.args[2].name == "id":
                    sig.set_type(2, "wx._WindowID")
            elif issubclass(cls, wx.Event):
                for i in range(1, min(3, len(sig.args))):
                    if "Type" in sig.args[i].name:
                        sig.set_type(i, "wx._EventType")
                    elif sig.args[i].name in ("id", "winid", "windid"):
                        sig.set_type(i, "wx._WindowID")
        if len(sigs) == 1:
            sigs[0].print(file=file)
        elif len(sigs) > 1:
            for sig in sigs:
                print("    @overload", file=file)
                sig.print(file=file)
    else: # at last resort use inspect.signature to get argument names if posible.
        s = signature_using_inspect(cls.__init__)  # type: ignore  # direct acsses of __init__
        if s not in deafult_signatures:
            print("    def __init__", s, " -> None: ...", sep="", file=file)

    # Now do the rest of the methods ...
    setters: dict[str, str] = {}
    for name in sorted(objects_by_category["method"]):
        doc = getattr(cls, name).__doc__  # get docstring from method not descriptor
        sigs = find_signatures(doc, name, add_any=add_any, meth=True)
        if not sigs:
            s = signature_using_inspect(getattr(cls, name), add_any=add_any)
            print("    def ", name, s, " -> Any: ...", sep="", file=file)
        elif len(sigs) == 1:
            (sig,) = sigs
            # infer setter type from getter
            if sig.name.startswith("Get"):
                t = convert_arg_type.get(sig.retanot.removeprefix("wx."), sig.retanot)
                setters[sig.name.replace("Get", "Set")] = t
            elif (sig.name in setters
                  and len(sig.args) == 2
                  and sig.args[1].tp in (None, "Any")):
                sig.set_type(1, setters.pop(name))
            sig.print(file=file)
        else:
            for sig in sigs:
                print("    @overload", file=file)
                sig.print(file=file)
    
    # ... the propertes ...
    for name in sorted(objects_by_category["property"]):        
        print("    ", name, " = property(", sep="", end="", file=file)
        if (g := "Get"+name) in cls_dict or (g := "Is"+name) in cls_dict:
            print(g, sep="", end="", file=file)
        else:
            print("None", sep="", end="", file=file)
        if (s := "Set"+name) in cls_dict:
            print(", ", s, sep="", end="", file=file)
        print(")", file=file)
    
    # ... and the enumerations.
    for name in objects_by_category["enumclass"]:
        print("    class ", name, "(wx._WxSipEnumtype):", sep="", file=file)
        cls = cls_dict[name]
        elements = sorted(name for name, obj in cls_dict.items() if isinstance(obj, cls))
        for element in elements:
            print("        ", element, " = ...", sep="", file=file)
            objects_by_category["other"].remove(element)
        for element in elements:
            #XXX print("    ", element, ": Literal[", name, ".", element, "]" sep="", file=buffer)
            # Seems mypy won't recognize Literal values from an enum
            # that's nested in a class, so we just do the type.
            print("    ", element, ": ", name, sep="", file=file)

    # finaly do any other objects (usualy an integer constant).
    for name in sorted(objects_by_category["other"]):
        print("    ", name, ": ", formatannotation(type(cls_dict[name])), sep="", file=file)

    
def make_function_stub(obj: type, file: TextIO, add_any: bool = False) -> None:
    sigs = find_signatures(obj.__doc__ or " ", obj.__name__, meth=False, add_any=add_any)
    if not sigs:
        print("def ", name, "(*args, **kwargs) -> Any: ...", sep="", file=file)
    elif len(sigs) == 1:
        sigs[0].print(file=file, indent="")
    else:
        for sig in sigs:
            print("@overload", file=file)
            sig.print(file=file, indent="")


def write_recursive(cls: type, file: TextIO, depth: int = 0, max_depth: int = -1,
                    add_any: bool = False, _seen: Optional[set[type]] = None):
    if _seen is None:
        _seen = set()
    make_stub(cls, file=file, add_any=add_any, hide_mod=_seen)
    depth += 1
    if depth == max_depth:
        return
    _seen.add(cls)
    subclasses = cls.__subclasses__()
    subclasses.sort(key=lambda c: c.__name__)
    for sub in subclasses:
        if sub.__name__ == cls.__name__:
            continue  # see wx.core.depriciated
        file.write("\n")
        write_recursive(sub, file, depth, max_depth, add_any, _seen)


############################################
if __name__ == "__main__":
    import sys
    import argparse
    parser = argparse.ArgumentParser(prog="stub-outliner")
    parser.add_argument("cls", nargs="+", metavar="class")
    parser.add_argument("--file", metavar="PATH")
    parser.add_argument("--add_any", action="store_true")
    parser.add_argument("--subclasses", action="store_true")
    parser.add_argument("--all_methods", action="store_true")
    args = parser.parse_args()
    if args.file:
        file = open(args.file, mode="a", encoding="utf-8")
    else:
        file = sys.stdout
    for name in args.cls:
        obj = eval(name, wx.__dict__)
        if args.subclasses and isinstance(obj, type):
            write_recursive(obj, file, add_any=args.add_any)
        elif isinstance(obj, type):
            make_stub(obj, add_any=args.add_any, all_methods=args.all_methods, file=file)
        elif callable(obj) and hasattr(obj, "__name__") and hasattr(obj, "__doc__"):
            make_function_stub(obj, add_any=args.add_any, file=file)
        else:
            raise TypeError(f"unrecognized type: {type(obj).__qualname__}")
        file.write("\n")
    if args.file:
        file.close()
    

    
