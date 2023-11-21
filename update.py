
from typing import Optional, Sequence, Any, Union
import sys
import ast
from pathlib import Path
from collections import defaultdict
from shutil import copytree, rmtree
from types import ModuleType, BuiltinFunctionType, FunctionType

import wx

    
class Import:
    def __init__(self, name: str, mod: str) -> None:
        self.mod = mod
        self.name = name
    
    def __str__(self) -> str:
        return f"from {self.mod} import {self.name} as {self.name}"

class ImportGroup:
    def __init__(self, imports: Sequence[Import]) -> None:
        assert imports, "empty import"
        mod = imports[0].mod
        if not all(imp.mod == mod for imp in imports):
            raise ValueError("Modules don't match")
        self.mod = mod
        self.imports = imports
    
    def __str__(self) -> str:
        names = [f"{imp.name} as {imp.name}" for imp in self.imports]
        n = ", ".join(names)
        r = f"from {self.mod} import {n}"
        if len(r) <= 80:
            return r
        n = ",\n    ".join(names)
        return f"from {self.mod} import (\n    {n}\n    )"
    
class Anotation:
    def __init__(self, name: str, type: str, comment: str = "", *, data: Any = None) -> None:
        self.type = type
        self.name = name
        self.comment = comment
        if data is not None:
            # it's an error to acsses 'data' if it wasn't set in constructer.
            self.data = data
    
    def __str__(self) -> str:
        if self.comment:
            return f"{self.name}: {self.type}  # {self.comment}"
        else:
            return f"{self.name}: {self.type}"

class EnumValue:
    def __init__(self, name: str, cls: str) -> None:
        self.name = name
        self.cls = cls
    
    def __str__(self) -> str:
        return f"{self.cls}.{self.name}"

class EnumClass:
    def __init__(self, name: str, values: Optional[Sequence[EnumValue]] = None) -> None:
        if values is not None and not all(val.cls == name for val in values):
            raise ValueError("Enum classes don't match")
        self.values = values
        self.name = name
    
    def __str__(self) -> str:
        if self.values is None:
            raise ValueError("no values set")
        name = self.name
        lines = [f"class {name}(_WxSipEnumtype):"]
        for val in self.values:
            if val.cls != name:
                raise ValueError("Enum classes don't match")
            lines.append(f"    {val.name} = ...")
        lines.append("")
        for val in self.values:
            lines.append(f"{val.name}: Literal[{name}.{val.name}]")
        return "\n".join(lines)
    
class Class:
    def __init__(self, name: str, bases: tuple[str, ...]) -> None:
        self.name = name
        self.bases = bases
    
    def __str__(self) -> str:
        return f"class {self.name}({', '.join(self.bases)}): pass"

class Assignment:
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value
    
    def __str__(self) -> str:
        return f"{self.name} = {self.value}"


stub_dir = Path.cwd() / sys.argv[0] / ".." / "stubs"
init_path = stub_dir / "__init__.pyi"
mod_dict = wx.__dict__.copy()

# The `StockCursor` enumeration type is overwriten by a depriciated class,
# and the code below relies on all enum classes beeing avaliable in the namespace.
mod_dict["StockCursor"] = type(wx.CURSOR_NONE)

def check_has_anotations(classdef, modname):
    for node in classdef.body:
        if isinstance(node, ast.FunctionDef):
            missing = []
            args = node.args.posonlyargs + node.args.args
            decorators = [name.id for name in node.decorator_list]
            if "staticmethod" in decorators:
                if args and args[0].arg == "self":
                    msg = "Error: 'self' argument to staticmethod at {}.{}.{}"
                    print(msg.format(modname, classdef.name, node.name))
            else:
                if not args or args[0].arg != "self":
                    msg = "Error: missing 'self' argument to method at {}.{}.{}"
                    print(msg.format(modname, classdef.name, node.name))
            for arg in args:
                if arg.annotation is None and arg.arg != "self":
                    missing.append(arg.arg)
            if missing:
                msg = "Error: method {}.{}.{} is missing anotation for arguments: {}"
                print(msg.format(modname, classdef.name, node.name, ", ".join(missing)))

# Import classes and functions from fake sub-modules.
classes_stubed: set[str] = set()
modules = [p for p in stub_dir.glob("_*.pyi") if p.stem.isidentifier() and p.stem[-1] != "_"]
def_import: list[ImportGroup] = []
for mod in modules:
    mod_node = ast.parse(mod.read_text(encoding="utf-8"), filename=str(mod))
    i: list[Import] = []
    last_overload = ""
    for node in mod_node.body:
        if isinstance(node, ast.ClassDef) and (name:=node.name)[0] != "_":
            check_has_anotations(node, mod.stem)
            i.append(Import(name=name, mod="wx."+mod.stem))
            try:
                mod_dict.pop(name)
            except KeyError:
                if node.name in classes_stubed:
                    print(f"Error: duplicate class definition - {node.name}")
                else:
                    print(f"Error: class {mod.stem}.{name} does not"
                           " corespond to an object in the wx namespace\n")
            else:
                classes_stubed.add(name)
        elif isinstance(node, ast.FunctionDef) and (name:=node.name)[0] != "_":
            if node.decorator_list:
                assert node.decorator_list[0].id == "overload"  # type: ignore
                if last_overload == name:
                    continue
                else:
                    last_overload = name
            i.append(Import(name=name, mod="wx."+mod.stem))
            try:
                mod_dict.pop(name)
            except KeyError:
                print(f"Error: function {mod.stem}.{name} does not"
                       " corespond to an object in the wx namespace\n")
        elif isinstance(node, ast.AnnAssign) and (name:=node.target.id)[0] != "_":  # type: ignore
            i.append(Import(name=name, mod="wx."+mod.stem))
            # don't raise error here becuse some constants are defined at App() initalization.
            mod_dict.pop(name, None)
    if i:
        def_import.append(ImportGroup(i))
    else:
        print(f"Error: nothing to import from module: {mod.stem}")

sipenum = type(wx.Alignment)
siptype = type(wx.Object)

def_undef: list[Anotation] = []  # type Any anotations
def_const: list[Anotation] = [] # constants
def_evt: list[Anotation] = [] # event type codes
def_err: list[Union[Class, Assignment]] = [] # Exception subclasses
def_enum: list[EnumClass] = [] # enumerations
def_val: dict[str, list[EnumValue]] = defaultdict(list)


overloads = [
    ("PlatformInfo", "tuple[str, ...]"),
    ("VERSION", "tuple[int, int, int, str]"),
    ("TIMER_CONTINUOUS", "bool"),
    ("TIMER_ONE_SHOT", "bool"),
]
for name, anot in overloads:
    mod_dict.pop(name)
    def_const.append(Anotation(name, anot, data=3))
remove = [
    "PyDataObjectSimple",
    "PyRegionIterator",
    "deprecated",
    "deprecatedMsg",
    "ImageArray",
    'CommandList',
    'FileHistoryMenuList',
    'MenuItemList',
    'MenuList',
    'PointList',
    'SizerItemList',
    'WindowList',
]
for name in remove:
    mod_dict.pop(name)

for name, obj in mod_dict.items():
    if name.startswith("_") or name.startswith("test") or name.endswith("_iterator"):
        continue
    tp = type(obj)
    if name.startswith("wxEVT"):
        def_evt.append(Anotation(name, "_EventType"))
    elif tp is sipenum:
        def_enum.append(EnumClass(name, []))
    elif tp is int:
        def_const.append(Anotation(name, f"Literal[{obj!r}]", data=1))
    elif tp in (str, bytes):
        def_const.append(Anotation(name, f"Literal[{obj!r}]", data=2))
    elif isinstance(tp, sipenum):
        def_val[tp.__name__].append(EnumValue(name, tp.__name__))
    elif tp is wx.PyEventBinder:
        def_const.append(Anotation(name, tp.__name__, data=4))
    elif isinstance(tp, siptype):
        def_const.append(Anotation(name, tp.__name__, data=3))
    elif tp is type and issubclass(obj, BaseException):
        if name == obj.__name__:
            def_err.append(Class(name, tuple(c.__name__ for c in obj.__bases__)))
        else:
            def_err.append(Assignment(name, obj.__name__))
    elif tp in (type, siptype):
        if "DeprecatedClassProxy" in obj.__qualname__:
            continue
        if issubclass(obj, wx.Dialog):
            comment = "dialog"
        elif issubclass(obj, wx.Control):
            comment = "control"
        elif issubclass(obj, wx.Window):
            comment = "window"
        else:
            comment = ""
        def_undef.append(Anotation(name, "Any", comment))
    elif tp in (BuiltinFunctionType, FunctionType):
        if "deprecated_func" in obj.__qualname__:
            continue
        def_undef.append(Anotation(name, "Any", "function"))
    elif tp is ModuleType:
        continue
    else:
        print("Error: Encountered unknown object of type "
              f"'{tp.__name__}' at name '{name}': {obj!r}")

for enum in def_enum:
    enum.values = def_val.pop(enum.name)

for defs in (def_undef, def_enum, def_const, def_evt, def_err):
    defs.sort(key=lambda obj: obj.name)  # type: ignore  # mypy dosen't know the type of 'defs'
# sort constants by type, but don't cach the literal value in Literal types.
def_const.sort(key=lambda obj: obj.type[:7])
def_const.sort(key=lambda obj: obj.data)
# Put assignments at end:
def_err.sort(key=lambda obj: type(obj) is not Class)

template = """
# This stub file is automaticly generated. Do not modify.

from typing import Type, Literal, NewType, Any, SupportsInt
from enum import IntFlag

# unstubbed classes and functions:
{undef}

_WindowID = SupportsInt

{imp}


{err}


class _WxSipEnumtype(IntFlag):
    \"""Drop in for sip.enumtype
    
    wx uses sip.enumtype internaly for enumerations,
    tecnicaly this is not a subclass of `int` or `Enum`,
    but it behaves like one.
    \"""

{enum}

# constants:
{const}

_EventType = NewType('_EventType', int)

{evt}

"""

with init_path.open(mode="w", encoding="utf-8") as file:
    file.write(template.format(undef="\n".join(str(x) for x in def_undef),
                               imp="\n".join(str(x) for x in def_import),
                               err="\n\n".join(str(x) for x in def_err),
                               enum="\n\n".join(str(x) for x in def_enum),
                               const="\n".join(str(x) for x in def_const),
                               evt="\n".join(str(x) for x in def_evt)))
done = len(classes_stubed)
all_ = done+len(def_undef)
print(f"{done} out of {all_} classes or functions done ({100*done//all_}%).")

wx_pkg = Path(wx.__path__[0])  # type: ignore  # "__path__" not guarantied to exist.
assert wx_pkg.name == "wx"
pkg_stubs = wx_pkg / ".." / "wx-stubs"
rmtree(pkg_stubs)
copytree(stub_dir, pkg_stubs)
