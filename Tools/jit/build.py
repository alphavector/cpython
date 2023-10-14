"""A template JIT for CPython 3.13, based on copy-and-patch."""

import asyncio
import dataclasses
import enum
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import typing

if sys.version_info < (3, 11):
    raise RuntimeError("Building the JIT compiler requires Python 3.11 or newer!")

TOOLS_JIT_BUILD = pathlib.Path(__file__).resolve()
TOOLS_JIT = TOOLS_JIT_BUILD.parent
TOOLS = TOOLS_JIT.parent
ROOT = TOOLS.parent
INCLUDE = ROOT / "Include"
INCLUDE_INTERNAL = INCLUDE / "internal"
PC = ROOT / "PC"
PC_PYCONFIG_H = PC / "pyconfig.h"
PYCONFIG_H = ROOT / "pyconfig.h"
PYTHON = ROOT / "Python"
PYTHON_EXECUTOR_CASES_C_H = PYTHON / "executor_cases.c.h"
PYTHON_JIT_STENCILS_H = PYTHON / "jit_stencils.h"
TOOLS_JIT_TEMPLATE_C = TOOLS_JIT / "template.c"

STUBS = ["deoptimize", "error", "oparg", "operand", "trampoline"]


HoleKind: typing.TypeAlias = typing.Literal[
    "R_386_32",
    "R_386_PC32",
    "R_AARCH64_ABS64",
    "R_AARCH64_ADR_GOT_PAGE",
    "R_AARCH64_CALL26",
    "R_AARCH64_JUMP26",
    "R_AARCH64_LD64_GOT_LO12_NC",
    "R_AARCH64_MOVW_UABS_G0_NC",
    "R_AARCH64_MOVW_UABS_G1_NC",
    "R_AARCH64_MOVW_UABS_G2_NC",
    "R_AARCH64_MOVW_UABS_G3",
    "R_X86_64_64",
    "R_X86_64_GOTOFF64",
    "R_X86_64_GOTPC32",
    "R_X86_64_GOTPCRELX",
    "R_X86_64_PC32",
    "R_X86_64_PLT32",
    "R_X86_64_REX_GOTPCRELX",
]


class ValueType(typing.TypedDict):
    Value: str
    RawValue: int


class RelocationTypeType(typing.TypedDict):
    Value: HoleKind
    RawValue: int


class Flag(typing.TypedDict):
    Name: str
    Value: int


class Flags(typing.TypedDict):
    RawFlags: int
    Flags: list[Flag]


class SectionData(typing.TypedDict):
    Offset: int
    Bytes: list[int]


class RelocationType(typing.TypedDict):
    Offset: int
    Type: RelocationTypeType
    Symbol: ValueType
    Addend: int


RelocationsType = list[dict[typing.Literal["Relocation"], RelocationType]]


class SymbolType(typing.TypedDict):
    Name: ValueType
    Value: int
    Size: int
    Binding: ValueType
    Type: ValueType
    Other: int
    Section: ValueType


SymbolsType = list[dict[typing.Literal["Symbol"], SymbolType]]


class SectionType(typing.TypedDict):
    Index: int
    Name: ValueType
    Type: ValueType
    Flags: Flags
    Address: int
    Offset: int
    Size: int
    Link: int
    Info: int
    AddressAlignment: int
    EntrySize: int
    Relocations: RelocationsType
    Symbols: SymbolsType
    SectionData: SectionData


class FileSummaryType(typing.TypedDict):
    File: str
    Format: str
    Arch: str
    AddressSize: int
    LoadName: str


SectionsType = list[dict[typing.Literal["Section"], SectionType]]


class FileType(typing.TypedDict):
    FileSummary: FileSummaryType
    Sections: SectionsType


ObjectType = list[dict[str, FileType] | FileType]


@enum.unique
class HoleValue(enum.Enum):
    _JIT_BODY = enum.auto()
    _JIT_CONTINUE = enum.auto()
    _JIT_DATA = enum.auto()
    _JIT_DEOPTIMIZE = enum.auto()
    _JIT_ERROR = enum.auto()
    _JIT_JUMP = enum.auto()
    _JIT_OPARG = enum.auto()
    _JIT_OPERAND = enum.auto()
    _JIT_ZERO = enum.auto()


@dataclasses.dataclass(frozen=True)
class Hole:
    offset: int
    kind: HoleKind
    value: HoleValue
    symbol: str | None
    addend: int


@dataclasses.dataclass(frozen=True)
class Stencil:
    body: bytearray
    holes: list[Hole]
    disassembly: list[str]
    data: bytearray
    holes_data: list[Hole]
    disassembly_data: list[str]


S = typing.TypeVar("S", bound=typing.Literal["Section", "Relocation", "Symbol"])
T = typing.TypeVar("T")


def unwrap(source: list[dict[S, T]], wrapper: S) -> list[T]:
    return [child[wrapper] for child in source]


def get_llvm_tool_version(name: str) -> int | None:
    try:
        args = [name, "--version"]
        process = subprocess.run(args, check=True, stdout=subprocess.PIPE)
    except FileNotFoundError:
        return None
    match = re.search(rb"version\s+(\d+)\.\d+\.\d+\s+", process.stdout)
    return int(match.group(1)) if match else None


def find_llvm_tool(tool: str) -> str | None:
    versions = {14, 15, 16}
    forced_version = os.getenv("PYTHON_LLVM_VERSION")
    if forced_version:
        versions &= {int(forced_version)}
    # Unversioned executables:
    path = tool
    version = get_llvm_tool_version(path)
    if version in versions:
        return path
    for version in sorted(versions, reverse=True):
        # Versioned executables:
        path = f"{tool}-{version}"
        if get_llvm_tool_version(path) == version:
            return path
        # My homebrew homies:
        try:
            args = ["brew", "--prefix", f"llvm@{version}"]
            process = subprocess.run(args, check=True, stdout=subprocess.PIPE)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        else:
            prefix = process.stdout.decode().removesuffix("\n")
            path = f"{prefix}/bin/{tool}"
            if get_llvm_tool_version(path) == version:
                return path
    return None


def require_llvm_tool(tool: str) -> str:
    path = find_llvm_tool(tool)
    if path is not None:
        return path
    raise RuntimeError(f"Can't find {tool}!")


_SEMAPHORE = asyncio.BoundedSemaphore(os.cpu_count() or 1)


async def run(*args: str | os.PathLike[str], capture: bool = False) -> bytes | None:
    async with _SEMAPHORE:
        print(shlex.join(map(str, args)))
        process = await asyncio.create_subprocess_exec(
            *args, stdout=subprocess.PIPE if capture else None, cwd=ROOT
        )
        stdout, stderr = await process.communicate()
    assert stderr is None, stderr
    if process.returncode:
        raise RuntimeError(f"{args[0]} exited with {process.returncode}")
    return stdout


class Parser:
    _ARGS = [
        "--elf-output-style=JSON",
        "--expand-relocs",
        # "--pretty-print",
        "--section-data",
        "--section-relocations",
        "--section-symbols",
        "--sections",
    ]

    def __init__(self, path: pathlib.Path, readobj: str, objdump: str | None) -> None:
        self.path = path
        self.body = bytearray()
        self.data = bytearray()
        self.body_symbols: dict[str, int] = {}
        self.data_symbols: dict[str, int] = {}
        self.body_offsets: dict[int, int] = {}
        self.data_offsets: dict[int, int] = {}
        self.got: dict[str, int] = {}
        self.body_relocations: list[tuple[int, RelocationType]] = []
        self.data_relocations: list[tuple[int, RelocationType]] = []
        self.readobj = readobj
        self.objdump = objdump

    async def parse(self) -> Stencil:
        if self.objdump is not None:
            output = await run(
                self.objdump, self.path, "--disassemble", "--reloc", capture=True
            )
            assert output is not None
            disassembly = [
                line.expandtabs().strip() for line in output.decode().splitlines()
            ]
            disassembly = [line for line in disassembly if line]
        else:
            disassembly = []
        output = await run(self.readobj, *self._ARGS, self.path, capture=True)
        assert output is not None
        self._data: ObjectType = json.loads(output)
        file = self._data[0]
        if str(self.path) in file:
            file = typing.cast(dict[str, FileType], file)[str(self.path)]
        for section in unwrap(typing.cast(SectionsType, file["Sections"]), "Section"):
            self._handle_section(section)
        if "_JIT_ENTRY" in self.body_symbols:
            entry = self.body_symbols["_JIT_ENTRY"]
        else:
            entry = self.body_symbols["_JIT_TRAMPOLINE"]
        assert entry == 0, entry
        holes = []
        holes_data = []
        padding = 0
        while len(self.body) % 8:  # XXX
            self.body.append(0)
            padding += 1
        offset_data = 0
        disassembly_data = []
        padding_data = 0
        if self.data:
            disassembly_data.append(f"{offset_data:x}: {str(bytes(self.data)).removeprefix('b')}")
            offset_data += len(self.data)
        while len(self.data) % 8:
            self.data.append(0)
            padding_data += 1
        if padding_data:
            disassembly_data.append(f"{offset_data:x}: {' '.join(padding_data * ['00'])}")
            offset_data += padding_data
        got = len(self.data)
        for base, relocation in self.body_relocations:
            newhole = self._handle_relocation(base, relocation)
            if newhole is None:
                continue
            if newhole.symbol in self.data_symbols:
                addend = newhole.addend + self.data_symbols[newhole.symbol]
                newhole = Hole(newhole.offset, newhole.kind, HoleValue._JIT_DATA, None, addend)
            elif newhole.symbol in self.body_symbols:
                addend = newhole.addend + self.body_symbols[newhole.symbol]
                newhole = Hole(newhole.offset, newhole.kind, HoleValue._JIT_BODY, None, addend)
            holes.append(newhole)
        for base, relocation in self.data_relocations:
            newhole = self._handle_relocation(base, relocation)
            if newhole is None:
                continue
            if newhole.symbol in self.data_symbols:
                addend = newhole.addend + self.data_symbols[newhole.symbol]
                newhole = Hole(newhole.offset, newhole.kind, HoleValue._JIT_DATA, None, addend)
            elif newhole.symbol in self.body_symbols:
                addend = newhole.addend + self.body_symbols[newhole.symbol]
                newhole = Hole(newhole.offset, newhole.kind, HoleValue._JIT_BODY, None, addend)
            holes_data.append(newhole)
        offset = len(self.body) - padding
        if padding:
            disassembly.append(f"{offset:x}: {' '.join(padding * ['00'])}")
            offset += padding
        assert offset == len(self.body), (offset, len(self.body))
        for s, got_offset in self.got.items():
            if s in self.body_symbols:
                addend = self.body_symbols[s]
                value, symbol = HoleValue._JIT_BODY, None
            elif s in self.data_symbols:
                addend = self.data_symbols[s]
                value, symbol = HoleValue._JIT_DATA, None
            else:
                value, symbol = self._symbol_to_value(s)
                addend = 0
            # XXX: R_386_32 on 32-bit platforms?
            holes_data.append(Hole(got + got_offset, "R_X86_64_64", value, symbol, addend))
            value_part = value.name if value is not HoleValue._JIT_ZERO else ""
            if value_part and not symbol and not addend:
                addend_part = ""
            else:
                addend_part = format_addend(symbol, addend)
                if value_part:
                    value_part += "+"
            disassembly_data.append(f"{offset_data:x}: {value_part}{addend_part}")
            offset_data += 8
        self.data.extend([0] * 8 * len(self.got))
        holes.sort(key=lambda hole: hole.offset)
        holes_data = [Hole(hole.offset, hole.kind, hole.value, hole.symbol, hole.addend) for hole in holes_data]
        holes_data.sort(key=lambda hole: hole.offset)
        assert offset_data == len(self.data), (offset_data, len(self.data), self.data, disassembly_data)
        return Stencil(self.body, holes, disassembly, self.data, holes_data, disassembly_data)

    def _handle_section(self, section: SectionType) -> None:
        type = section["Type"]["Value"]
        flags = {flag["Name"] for flag in section["Flags"]["Flags"]}
        if type in {"SHT_REL", "SHT_RELA"}:
            assert "SHF_INFO_LINK" in flags, flags
            assert not section["Symbols"]
            if section["Info"] in self.body_offsets:
                base = self.body_offsets[section["Info"]]
                for relocation in unwrap(section["Relocations"], "Relocation"):
                    self.body_relocations.append((base, relocation))
            else:
                base = self.data_offsets[section["Info"]]
                for relocation in unwrap(section["Relocations"], "Relocation"):
                    self.data_relocations.append((base, relocation))
        elif type == "SHT_PROGBITS":
            if "SHF_ALLOC" not in flags:
                return
            elif flags & {"SHF_EXECINSTR"}:
                self.body_offsets[section["Index"]] = len(self.body)
                for symbol in unwrap(section["Symbols"], "Symbol"):
                    offset = len(self.body) + symbol["Value"]
                    name = symbol["Name"]["Value"]
                    assert name not in self.body_symbols
                    self.body_symbols[name] = offset
                section_data = section["SectionData"]
                self.body.extend(section_data["Bytes"])
            else:
                self.data_offsets[section["Index"]] = len(self.data)
                for symbol in unwrap(section["Symbols"], "Symbol"):
                    offset = len(self.data) + symbol["Value"]
                    name = symbol["Name"]["Value"]
                    assert name not in self.data_symbols
                    self.data_symbols[name] = offset
                section_data = section["SectionData"]
                self.data.extend(section_data["Bytes"])
            assert not section["Relocations"]
        else:
            assert type in {
                "SHT_GROUP",
                "SHT_LLVM_ADDRSIG",
                "SHT_NULL",
                "SHT_STRTAB",
                "SHT_SYMTAB",
            }, type

    def read_u32(self, offset: int) -> int:
        return int.from_bytes(self.body[offset : offset + 4], "little")

    def _got_lookup(self, symbol: str | None) -> int:
        while len(self.data) % 8:
            self.data.append(0)
        if symbol is None:
            return len(self.data)
        return len(self.data) + self.got.setdefault(symbol, 8 * len(self.got))

    @staticmethod
    def _symbol_to_value(symbol: str) -> tuple[HoleValue, str | None]:
        try:
            return HoleValue[symbol], None
        except KeyError:
            return HoleValue._JIT_ZERO, symbol

    def _handle_relocation(self, base: int, relocation: RelocationType) -> Hole | None:
        match relocation:
            case {
                "Type": {
                    "Value": "R_AARCH64_ADR_GOT_PAGE"
                    | "R_AARCH64_LD64_GOT_LO12_NC" as kind
                },
                "Symbol": {"Value": s},
                "Offset": offset,
                "Addend": addend,
            }:
                offset += base
                value, symbol = HoleValue._JIT_DATA, None
                addend += self._got_lookup(s)
            case {
                "Type": {"Value": "R_X86_64_GOTOFF64" as kind},
                "Symbol": {"Value": s},
                "Offset": offset,
                "Addend": addend,
            }:
                offset += base
                value, symbol = self._symbol_to_value(s)  # XXX
                addend -= len(self.body) - offset  # XXX
            case {
                "Type": {"Value": "R_X86_64_GOTPC32" as kind},
                "Symbol": {"Value": "_GLOBAL_OFFSET_TABLE_"},
                "Offset": offset,
                "Addend": addend,
            }:
                offset += base
                value, symbol = HoleValue._JIT_DATA, None
                addend += self._got_lookup(None)
            case {
                "Type": {"Value": "R_X86_64_GOTPCRELX" | "R_X86_64_REX_GOTPCRELX" as kind},
                "Symbol": {"Value": s},
                "Offset": offset,
                "Addend": addend,
            }:
                offset += base
                value, symbol = HoleValue._JIT_DATA, None
                addend += self._got_lookup(s)
            case {
                "Type": {"Value": kind},
                "Symbol": {"Value": s},
                "Offset": offset,
                "Addend": addend,
            }:
                offset += base
                value, symbol = self._symbol_to_value(s)
            case {
                "Type": {"Value": "R_386_32" | "R_386_PC32" as kind},
                "Symbol": {"Value": s},
                "Offset": offset,
            }:
                offset += base
                value, symbol = self._symbol_to_value(s)
                addend = self.read_u32(offset)  # XXX
            case _:
                raise NotImplementedError(relocation)
        return Hole(offset, kind, value, symbol, addend)


@dataclasses.dataclass(frozen=True)
class Target:
    pattern: str
    frontend: str
    backend: str
    model: str
    ghccc: bool
    pyconfig: pathlib.Path


TARGETS = [
    Target(
        pattern=r"aarch64-apple-darwin.*",
        frontend="aarch64-apple-darwin",
        backend="aarch64-elf",
        model="large",
        ghccc=False,
        pyconfig=PYCONFIG_H,
    ),
    Target(
        pattern=r"aarch64-.*-linux-gnu",
        frontend="aarch64-unknown-linux-gnu",
        backend="aarch64-elf",
        model="large",
        ghccc=False,
        pyconfig=PYCONFIG_H,
    ),
    Target(
        pattern=r"i686-pc-windows-msvc",
        frontend="i686-pc-windows-msvc",
        backend="i686-pc-windows-msvc-elf",
        model="small",
        ghccc=True,
        pyconfig=PC_PYCONFIG_H,
    ),
    Target(
        pattern=r"x86_64-apple-darwin.*",
        frontend="x86_64-apple-darwin",
        backend="x86_64-elf",
        model="medium",
        ghccc=True,
        pyconfig=PYCONFIG_H,
    ),
    Target(
        pattern=r"x86_64-pc-windows-msvc",
        frontend="x86_64-pc-windows-msvc",
        backend="x86_64-pc-windows-msvc-elf",
        model="medium",
        ghccc=True,
        pyconfig=PC_PYCONFIG_H,
    ),
    Target(
        pattern=r"x86_64-.*-linux-gnu",
        frontend="x86_64-unknown-linux-gnu",
        backend="x86_64-elf",
        model="medium",
        ghccc=True,
        pyconfig=PYCONFIG_H,
    ),
]


def get_target(host: str) -> Target:
    for target in TARGETS:
        if re.fullmatch(target.pattern, host):
            return target
    raise NotImplementedError(host)


CFLAGS = [
    "-O3",
    "-Wno-override-module",
    # Keep library calls from sneaking in:
    "-ffreestanding",  # XXX
    # Position-independent code adds indirection to every load and jump:
    "-fno-pic",
    # The GHC calling convention uses %rbp as an argument-passing register:
    "-fomit-frame-pointer",  # XXX
]

CPPFLAGS = [
    f"-DPy_BUILD_CORE",
    f"-D_PyJIT_ACTIVE",
    f"-I{INCLUDE}",
    f"-I{INCLUDE_INTERNAL}",
    f"-I{PYTHON}",
]


class Compiler:
    def __init__(
        self,
        *,
        verbose: bool = False,
        ghccc: bool,
        target: Target,
    ) -> None:
        self._stencils_built: dict[str, Stencil] = {}
        self._verbose = verbose
        self._clang = require_llvm_tool("clang")
        self._readobj = require_llvm_tool("llvm-readobj")
        self._objdump = find_llvm_tool("llvm-objdump")
        self._ghccc = ghccc
        self._target = target

    def _use_ghccc(self, ll: pathlib.Path) -> None:
        """LLVM's GHCC calling convention is perfect for our needs"""
        # TODO: Explore
        if self._ghccc:
            before = ll.read_text()
            after = re.sub(
                r"((?:noalias )?(?:ptr|%struct._PyInterpreterFrame\*) @_JIT_(?:CONTINUE|DEOPTIMIZE|ENTRY|JUMP)\b)",
                r"ghccc \1",
                before,
            )
            assert before != after, after
            ll.write_text(after)

    async def _compile(
        self, opname: str, c: pathlib.Path, tempdir: pathlib.Path
    ) -> None:
        ll = tempdir / f"{opname}.ll"
        o = tempdir / f"{opname}.o"
        backend_flags = [
            *CFLAGS,
            f"--target={self._target.backend}",
            f"-c",
            # We have three options for code model:
            # - "small": assumes that code and data reside in the lowest 2GB of
            #   memory (128MB on aarch64)
            # - "medium": assumes that code resides in the lowest 2GB of memory,
            #   and makes no assumptions about data (not available on aarch64)
            # - "large": makes no assumptions about either code or data
            # We need 64-bit addresses for data everywhere, but we'd *really*
            # prefer direct short jumps instead of indirect long ones where
            # possible. So, we use the "large" code model on aarch64 and the
            # "medium" code model elsewhere, which gives us correctly-sized
            # direct jumps and immediate data loads on basically all platforms:
            f"-mcmodel={self._target.model}",
        ]
        frontend_flags = [
            *CFLAGS,
            *CPPFLAGS,
            f"--target={self._target.frontend}",
            f"-D_DEBUG" if sys.argv[2:] == ["-d"] else "-DNDEBUG",  # XXX
            f"-D_JIT_OPCODE={opname}",
            f"-I{self._target.pyconfig.parent}",
            f"-S",
            f"-emit-llvm",
        ]
        await run(self._clang, *frontend_flags, "-o", ll, c)
        self._use_ghccc(ll)
        await run(self._clang, *backend_flags, "-o", o, ll)
        self._stencils_built[opname] = await Parser(
            o, self._readobj, self._objdump
        ).parse()

    async def build(self) -> None:
        generated_cases = PYTHON_EXECUTOR_CASES_C_H.read_text()
        opnames = sorted(re.findall(r"\n {8}case (\w+): \{\n", generated_cases))
        with tempfile.TemporaryDirectory() as tempdir:
            work = pathlib.Path(tempdir).resolve()
            async with asyncio.TaskGroup() as group:
                for stub in STUBS:
                    task = self._compile(stub, TOOLS_JIT / f"{stub}.c", work)
                    group.create_task(task)
                for opname in opnames:
                    task = self._compile(opname, TOOLS_JIT_TEMPLATE_C, work)
                    group.create_task(task)


def format_addend(symbol: str | None, addend: int) -> str:
    symbol_part = f"(uintptr_t)&{symbol}" if symbol else ""
    addend %= 1 << 64
    if symbol_part and not addend:
        return symbol_part
    if addend & (1 << 63):
        return f"{symbol_part}{hex(addend - (1 << 64))}"
    return f"{f'{symbol_part}+' if symbol_part else ''}{hex(addend)}"


def dump(stencils: dict[str, Stencil]) -> typing.Generator[str, None, None]:
    yield f"// $ {sys.executable} {' '.join(map(shlex.quote, sys.argv))}"  # XXX
    yield f""
    yield f"typedef enum {{"
    for kind in sorted(typing.get_args(HoleKind)):
        yield f"    {kind},"
    yield f"}} HoleKind;"
    yield f""
    yield f"typedef enum {{"
    for value in HoleValue:
        yield f"    {value.name},"
    yield f"}} HoleValue;"
    yield f""
    yield f"typedef struct {{"
    yield f"    const uint64_t offset;"
    yield f"    const HoleKind kind;"
    yield f"    const HoleValue value;"
    yield f"    const uint64_t addend;"
    yield f"}} Hole;"
    yield f""
    yield f"typedef struct {{"
    yield f"    const size_t nbytes;"
    yield f"    const unsigned char * const bytes;"
    yield f"    const size_t nholes;"
    yield f"    const Hole * const holes;"
    yield f"    const size_t nbytes_data;"
    yield f"    const unsigned char * const bytes_data;"
    yield f"    const size_t nholes_data;"
    yield f"    const Hole * const holes_data;"
    yield f"}} Stencil;"
    yield f""
    opnames = []
    for opname, stencil in sorted(stencils.items()):
        opnames.append(opname)
        yield f"// {opname}"
        assert stencil.body
        for line in stencil.disassembly:
            yield f"// {line}"
        body = ", ".join(f"0x{byte:02x}" for byte in stencil.body)
        yield f"static const unsigned char {opname}_stencil_bytes[{len(stencil.body)}] = {{{body}}};"
        if stencil.holes:
            yield f"static const Hole {opname}_stencil_holes[{len(stencil.holes) + 1}] = {{"
            for hole in sorted(stencil.holes, key=lambda hole: hole.offset):
                parts = [
                    hex(hole.offset),
                    hole.kind,
                    hole.value.name,
                    format_addend(hole.symbol, hole.addend),
                ]
                yield f"    {{{', '.join(parts)}}},"
            yield f"}};"
        else:
            yield f"static const Hole {opname}_stencil_holes[1];"
        for line in stencil.disassembly_data:
            yield f"// {line}"
        body = ", ".join(f"0x{byte:02x}" for byte in stencil.data)
        if stencil.data:
            yield f"static const unsigned char {opname}_stencil_bytes_data[{len(stencil.data) + 1}] = {{{body}}};"
        else:
            yield f"static const unsigned char {opname}_stencil_bytes_data[1];"
        if stencil.holes_data:
            yield f"static const Hole {opname}_stencil_holes_data[{len(stencil.holes_data) + 1}] = {{"
            for hole in sorted(stencil.holes_data, key=lambda hole: hole.offset):
                parts = [
                    hex(hole.offset),
                    hole.kind,
                    hole.value.name,
                    format_addend(hole.symbol, hole.addend),
                ]
                yield f"    {{{', '.join(parts)}}},"
            yield f"}};"
        else:
            yield f"static const Hole {opname}_stencil_holes_data[1];"
        yield f""
    yield f"#define INIT_STENCIL(OP) {{                             \\"
    yield f"    .nbytes = Py_ARRAY_LENGTH(OP##_stencil_bytes),     \\"
    yield f"    .bytes = OP##_stencil_bytes,                       \\"
    yield f"    .nholes = Py_ARRAY_LENGTH(OP##_stencil_holes) - 1, \\"
    yield f"    .holes = OP##_stencil_holes,                       \\"
    yield f"    .nbytes_data = Py_ARRAY_LENGTH(OP##_stencil_bytes_data) - 1, \\"
    yield f"    .bytes_data = OP##_stencil_bytes_data,                       \\"
    yield f"    .nholes_data = Py_ARRAY_LENGTH(OP##_stencil_holes_data) - 1, \\"
    yield f"    .holes_data = OP##_stencil_holes_data,                       \\"
    yield f"}}"
    yield f""
    assert opnames[-len(STUBS):] == STUBS
    for stub in opnames[-len(STUBS):]:
        yield f"static const Stencil {stub}_stencil = INIT_STENCIL({stub});"
    yield f""
    yield f"static const Stencil stencils[512] = {{"
    for opname in opnames[:-len(STUBS)]:
        yield f"    [{opname}] = INIT_STENCIL({opname}),"
    yield f"}};"
    yield f""
    yield f"#define GET_PATCHES() {{ \\"
    for value in HoleValue:
        yield f"    [{value.name}] = (uint64_t)0xBADBADBADBADBADB, \\"
    yield f"}}"


def main(host: str) -> None:
    target = get_target(host)
    hasher = hashlib.sha256(host.encode())
    hasher.update(PYTHON_EXECUTOR_CASES_C_H.read_bytes())
    hasher.update(target.pyconfig.read_bytes())
    for source in sorted(TOOLS_JIT.iterdir()):
        hasher.update(source.read_bytes())
    digest = hasher.hexdigest()
    if PYTHON_JIT_STENCILS_H.exists():
        with PYTHON_JIT_STENCILS_H.open() as file:
            if file.readline().removeprefix("// ").removesuffix("\n") == digest:
                return
    compiler = Compiler(verbose=True, ghccc=target.ghccc, target=target)
    asyncio.run(compiler.build())
    with PYTHON_JIT_STENCILS_H.open("w") as file:
        file.write(f"// {digest}\n")
        for line in dump(compiler._stencils_built):
            file.write(f"{line}\n")


if __name__ == "__main__":
    main(sys.argv[1])
