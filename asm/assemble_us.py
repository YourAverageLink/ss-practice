import glob
import re
from subprocess import call
import os
import tempfile
import shutil
from collections import OrderedDict
import struct
import yaml
import traceback

import sys

sys.path.insert(0, "../sslib")
from fs_helpers import *
from elf import *
from to_lst import create_lst
from relmapper import map_rel
from pyelf2rel import elf_to_rel

if sys.platform == "win32":
    devkitbasepath = r"C:\devkitPro\devkitPPC\bin"
else:
    if not "DEVKITPPC" in os.environ:
        raise Exception(
            r"Could not find devkitPPC. Path to devkitPPC should be in the DEVKITPPC env var."
        )
    devkitbasepath = os.environ.get("DEVKITPPC") + "/bin"


def get_bin(name):
    if not sys.platform == "win32":
        return os.path.join(devkitbasepath, name)
    return os.path.join(devkitbasepath, name + ".exe")


if not os.path.isfile(get_bin("powerpc-eabi-as")):
    raise Exception(
        r"Failed to assemble code: Could not find devkitPPC. devkitPPC should be installed to: C:\devkitPro\devkitPPC."
    )

# Allow yaml to dump OrderedDicts for the diffs.
yaml.CDumper.add_representer(
    OrderedDict, lambda dumper, data: dumper.represent_dict(data.items())
)

# Change how yaml dumps lists so each element isn't on a separate line.
yaml.CDumper.add_representer(
    list,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)

# Output integers as hexadecimal.
yaml.CDumper.add_representer(
    int, lambda dumper, data: yaml.ScalarNode("tag:yaml.org,2002:int", "0x%02X" % data)
)

temp_dir = tempfile.mkdtemp()
print(temp_dir)
print()

custom_symbols = OrderedDict()
custom_symbols["main.dol"] = OrderedDict()

with open("original_symbols/us.txt", "r") as f:
    original_symbols = yaml.safe_load(f)

with open("free_space_start_offsets/us.txt", "r") as f:
    free_space_start_offsets = yaml.safe_load(f)

next_free_space_offsets = {}
for file_path, offset in free_space_start_offsets.items():
    next_free_space_offsets[file_path] = offset


def get_code_and_relocations_from_elf(bin_name):
    elf = ELF()
    elf.read_from_file(bin_name)

    relocations_in_elf = []

    for elf_section in elf.sections:
        # TODO: Maybe support multiple sections, not just .text, such as .data?
        found_text_section = False
        if elf_section.name == ".text":
            assert not found_text_section
            found_text_section = True
            # Get the code and overwrite the ELF file with just the raw binary code.
            with open(bin_name, "wb") as f:
                f.write(read_all_bytes(elf_section.data))
        elif elf_section.type == ELFSectionType.SHT_RELA:
            # Get the relocations.
            assert elf_section.name.startswith(".rela")
            relocated_section_name = elf_section.name[len(".rela") :]
            assert relocated_section_name == ".text"

            for elf_relocation in elf.relocations[elf_section.name]:
                elf_symbol = elf.symbols[".symtab"][elf_relocation.symbol_index]
                is_local_relocation = try_apply_local_relocation(
                    bin_name, elf_relocation, elf_symbol
                )

                if not is_local_relocation:
                    relocations_in_elf.append(
                        OrderedDict(
                            [
                                ["SymbolName", elf_symbol.name],
                                ["Offset", elf_relocation.relocation_offset],
                                ["Type", elf_relocation.type.name],
                            ]
                        )
                    )

    return relocations_in_elf


def try_apply_local_relocation(bin_name, elf_relocation, elf_symbol):
    branch_label_match = re.search(
        r"^branch_label_([0-9A-F]+)$", elf_symbol.name, re.IGNORECASE
    )
    if branch_label_match:
        # We should relocate the relative branches within this REL ourselves so the game doesn't need to do it at runtime.
        branch_src_offset = org_offset + elf_relocation.relocation_offset
        branch_dest_offset = int(branch_label_match.group(1), 16)
        relative_branch_offset = ((branch_dest_offset - branch_src_offset) // 4) << 2

        if elf_relocation.type == ELFRelocationType.R_PPC_REL24:
            if (
                relative_branch_offset > 0x1FFFFFF
                or relative_branch_offset < -0x2000000
            ):
                raise Exception(
                    "Relocation failed: Cannot branch from %X to %X with a 24-bit relative offset."
                    % (branch_src_offset, branch_dest_offset)
                )

            with open(bin_name, "r+b") as f:
                instruction = read_u32(f, elf_relocation.relocation_offset)
                instruction &= ~0x03FFFFFC
                instruction |= relative_branch_offset & 0x03FFFFFC
                write_u32(f, elf_relocation.relocation_offset, instruction)

            return True
        elif elf_relocation.type == ELFRelocationType.R_PPC_REL14:
            if relative_branch_offset > 0x7FFF or relative_branch_offset < -0x8000:
                raise Exception(
                    "Relocation failed: Cannot branch from %X to %X with a 14-bit relative offset."
                    % (branch_src_offset, branch_dest_offset)
                )

            with open(bin_name, "r+b") as f:
                instruction = read_u32(f, elf_relocation.relocation_offset)
                instruction &= ~0x0000FFFC
                instruction |= relative_branch_offset & 0x0000FFFC
                write_u32(f, elf_relocation.relocation_offset, instruction)

            return True

    if elf_relocation.type == ELFRelocationType.R_PPC_ADDR32:
        # Also relocate absolute pointers into main.dol.

        with open(bin_name, "r+b") as f:
            write_u32(f, elf_relocation.relocation_offset, elf_symbol.address)

        return True

    return False


SDA_RE = re.compile(r"([a-z]+) (r[0-9]+), *([a-zA-Z0-9_]+)@sda21 *\(r13\).*")
SDA_13_BASE = 0x80579440  # US 1.0
# SDA_13_BASE = 0x8057c6a0 # JP 1.0
SDA_13_MAX = SDA_13_BASE + 0x7FFF
SDA_13_MIN = SDA_13_BASE - 0x8000


def handle_sda_instr(line: str) -> str:
    match = SDA_RE.match(line)
    if not match:
        raise Exception(line)
        return line
    instr = match.group(1)
    reg = match.group(2)
    lbl = match.group(3)
    address = original_symbols["main.dol"][lbl]
    if address < SDA_13_MIN or address > SDA_13_MAX:
        raise Exception(
            f"Relocation failed, SDA for symbol {elf_symbol.name} out of range."
        )
    if instr == "la":
        return f"addi {reg}, r13, {address-SDA_13_BASE}"
    else:
        return f"{instr} {reg}, {address-SDA_13_BASE}(r13)"


try:
    with open("linker.ld") as f:
        linker_script = f.read()

    # add main dol symbols
    for dol_sym, addr in original_symbols["main.dol"].items():
        linker_script += f"{dol_sym} = 0x{addr:X};\n"

    with open("asm_macros.asm") as f:
        asm_macros = f.read()

    all_asm_file_paths = glob.glob("./patches/us/*.asm")
    all_asm_files = [os.path.basename(rel_path) for rel_path in all_asm_file_paths]

    # First parse all the asm files into code chunks.
    code_chunks = OrderedDict()
    local_branches_linker_script_for_file = {}
    next_free_space_id_for_file = {}
    for patch_filename in all_asm_files:
        print("Assembling " + patch_filename)
        patch_path = os.path.join(".", "patches", "us", patch_filename)
        with open(patch_path) as f:
            asm = f.read()

        patch_name = os.path.splitext(patch_filename)[0]
        code_chunks[patch_name] = OrderedDict()

        most_recent_file_path = None
        most_recent_org_offset = None
        for line in asm.splitlines():
            line = re.sub(r";.+$", "", line)
            line = line.strip()

            open_file_match = re.match(r"\.open\s+\"([^\"]+)\"$", line, re.IGNORECASE)
            org_match = re.match(
                r"\.org\s+(0x[0-9a-f]+|@MainInjection)$", line, re.IGNORECASE
            )
            org_symbol_match = re.match(
                r"\.org\s+([\._a-z][\._a-z0-9]+|@NextFreeSpace)$", line, re.IGNORECASE
            )
            branch_match = re.match(
                r"(?:b|beq|bne|blt|bgt|ble|bge)\s+0x([0-9a-f]+)(?:$|\s)",
                line,
                re.IGNORECASE,
            )
            if open_file_match:
                relative_file_path = open_file_match.group(1)
                if most_recent_file_path or most_recent_org_offset is not None:
                    raise Exception(
                        "File %s was not closed before opening new file %s."
                        % (most_recent_file_path, relative_file_path)
                    )
                if relative_file_path not in code_chunks[patch_name]:
                    code_chunks[patch_name][relative_file_path] = OrderedDict()
                if relative_file_path not in local_branches_linker_script_for_file:
                    local_branches_linker_script_for_file[relative_file_path] = ""
                most_recent_file_path = relative_file_path
                continue
            elif org_match:
                if not most_recent_file_path:
                    raise Exception("Found .org directive when no file was open.")

                org_symbol = org_match.group(1)

                if org_symbol == "@MainInjection":
                    org_symbol = "0x80062e60"  # JP: 0x80062f40, US: 0x80062e60

                org_offset = int(org_symbol, 16)

                if org_offset >= free_space_start_offsets[most_recent_file_path]:
                    raise Exception(
                        'Tried to manually set the origin point to after the start of free space.\n.org offset: 0x%X\nFile path: %s\n\nUse ".org @NextFreeSpace" instead to get an automatically assigned free space offset.'
                        % (org_offset, most_recent_file_path)
                    )

                code_chunks[patch_name][most_recent_file_path][org_offset] = ""
                most_recent_org_offset = org_offset
                continue
            elif org_symbol_match:
                if not most_recent_file_path:
                    raise Exception("Found .org directive when no file was open.")

                org_symbol = org_symbol_match.group(1)

                if org_symbol == "@NextFreeSpace":
                    # Need to make each instance of @NextFreeSpace into a unique label.
                    if most_recent_file_path not in next_free_space_id_for_file:
                        next_free_space_id_for_file[most_recent_file_path] = 1
                    org_symbol = (
                        "@FreeSpace_%d"
                        % next_free_space_id_for_file[most_recent_file_path]
                    )
                    next_free_space_id_for_file[most_recent_file_path] += 1

                code_chunks[patch_name][most_recent_file_path][org_symbol] = ""
                most_recent_org_offset = org_symbol
                continue
            elif branch_match:
                # Replace branches to specific addresses with labels, and define the address of those labels in the linker script.
                branch_dest = int(branch_match.group(1), 16)
                branch_temp_label = "branch_label_%X" % branch_dest
                local_branches_linker_script_for_file[
                    most_recent_file_path
                ] += "%s = 0x%X;\n" % (branch_temp_label, branch_dest)
                line = re.sub(r"0x" + branch_match.group(1), branch_temp_label, line, 1)
            elif line == ".close":
                most_recent_file_path = None
                most_recent_org_offset = None
                continue
            elif not line:
                # Blank line
                continue

            if not most_recent_file_path:
                if line[0] == ";":
                    # Comment
                    continue
                raise Exception("Found code when no file was open.")
            if most_recent_org_offset is None:
                if line[0] == ";":
                    # Comment
                    continue
                raise Exception("Found code before any .org directive.")

            if "@sda21" in line:
                line = handle_sda_instr(line)

            code_chunks[patch_name][most_recent_file_path][most_recent_org_offset] += (
                line + "\n"
            )

        if not code_chunks[patch_name]:
            raise Exception("No code found.")

        if most_recent_file_path or most_recent_org_offset is not None:
            raise Exception(
                "File %s was not closed before the end of the file."
                % most_recent_file_path
            )

    for patch_name, code_chunks_for_patch in code_chunks.items():
        diffs = OrderedDict()

        for file_path, code_chunks_for_file in code_chunks_for_patch.items():
            if file_path not in custom_symbols:
                custom_symbols[file_path] = OrderedDict()
            custom_symbols_for_file = custom_symbols[file_path]

            # Sort code chunks in this patch so that free space chunks come first.
            # This is necessary so non-free-space chunks can branch to the free space chunks.
            def free_space_org_list_sorter(code_chunk_tuple):
                org_offset_or_symbol, temp_asm = code_chunk_tuple
                if isinstance(org_offset_or_symbol, int):
                    return 0
                else:
                    org_symbol = org_offset_or_symbol
                    free_space_match = re.search(r"@FreeSpace_\d+", org_symbol)
                    if free_space_match:
                        return -1
                    else:
                        return 0

            code_chunks_for_file_sorted = list(code_chunks_for_file.items())
            code_chunks_for_file_sorted.sort(key=free_space_org_list_sorter)

            temp_linker_script = linker_script + "\n"
            # Add custom symbols in the current file to the temporary linker script.
            for symbol_name, symbol_address in custom_symbols[file_path].items():
                temp_linker_script += "%s = 0x%08X;\n" % (symbol_name, symbol_address)
            # And add any local branches inside this file.
            temp_linker_script += local_branches_linker_script_for_file[file_path]
            if file_path != "main.dol":
                # Also add custom symbols in main.dol for all files.
                for symbol_name, symbol_address in custom_symbols["main.dol"].items():
                    temp_linker_script += "%s = 0x%08X;\n" % (
                        symbol_name,
                        symbol_address,
                    )

            for org_offset_or_symbol, temp_asm in code_chunks_for_file_sorted:
                is_custom_function = False
                if isinstance(org_offset_or_symbol, int):
                    org_offset = org_offset_or_symbol
                else:
                    org_symbol = org_offset_or_symbol
                    free_space_match = re.search(r"@FreeSpace_\d+", org_symbol)
                    if free_space_match:
                        is_custom_function = True
                        org_offset = next_free_space_offsets[file_path]
                    else:
                        if org_symbol not in custom_symbols_for_file:
                            raise Exception(
                                ".org specified an invalid custom symbol: %s."
                                % org_symbol
                            )
                        org_offset = custom_symbols_for_file[org_symbol]

                temp_linker_name = os.path.join(temp_dir, "tmp_linker.ld")
                with open(temp_linker_name, "w") as f:
                    f.write(temp_linker_script)

                temp_asm_name = os.path.join(
                    temp_dir, "tmp_" + patch_name + "_%08X.asm" % org_offset
                )
                with open(temp_asm_name, "w") as f:
                    f.write(
                        asm_macros
                    )  # Add our custom asm macros to all asm at the start.
                    f.write("\n")
                    f.write(temp_asm)

                o_name = os.path.join(
                    temp_dir, "tmp_" + patch_name + "_%08X.o" % org_offset
                )
                command = [
                    get_bin("powerpc-eabi-as"),
                    "-mregnames",
                    "-m750cl",
                    temp_asm_name,
                    "-o",
                    o_name,
                ]
                print(" ".join(command))
                print()
                result = call(command)
                if result != 0:
                    raise Exception("Assembler call failed.")

                bin_name = os.path.join(
                    temp_dir, "tmp_" + patch_name + "_%08X.bin" % org_offset
                )
                map_name = os.path.join(temp_dir, "tmp_" + patch_name + ".map")
                relocations = []
                command = [
                    get_bin("powerpc-eabi-ld"),
                    "-Ttext",
                    "%X" % org_offset,
                    "-T",
                    temp_linker_name,
                    "-Map=" + map_name,
                    o_name,
                    "-o",
                    bin_name,
                ]

                # add custom functions from rust
                if is_custom_function and file_path == "main.dol":
                    if result := call(["cargo", "fmt"], cwd="./custom-functions"):
                        raise Exception("Formatting rust functions failed.")
                    if result := call(
                        ["cargo", "build", "--features", "static", "--release"],
                        cwd="./custom-functions",
                    ):
                        raise Exception("Building rust main.dol functions failed.")

                    command.extend(
                        (
                            "-(",
                            "./custom-functions/target/powerpc-unknown-eabi/release/libcustom_functions.a",
                            "--gc-sections",
                            "--print-gc-sections",
                            "-)",
                        )
                    )

                if file_path.endswith(".rel"):
                    # Output an ELF with relocations for RELs.
                    command += ["--relocatable"]
                else:
                    # normally, just output the raw binary code, not an ELF.
                    # for the main custom function output an elf first so that the linker pruning works
                    if not is_custom_function:
                        command += ["--oformat", "binary"]
                    pass
                print(" ".join(command))
                print()
                result = call(command)
                if result != 0:
                    raise Exception("Linker call failed.")
                # Keep track of custom symbols so they can be passed in the linker script to future assembler calls.
                with open(map_name) as f:
                    for line in f.read().splitlines():
                        match = re.search(
                            r" +0x(?:00000000)?([0-9a-f]{8}) +([a-zA-Z]\S+)$", line
                        )
                        if not match:
                            continue
                        symbol_address = int(match.group(1), 16)
                        symbol_name = match.group(2)
                        custom_symbols_for_file[symbol_name] = symbol_address
                        temp_linker_script += "%s = 0x%08X;\n" % (
                            symbol_name,
                            symbol_address,
                        )

                if file_path.endswith(".rel"):
                    # This is for a REL, so we can't link it.
                    # Instead read the ELF to get the assembled code and relocations out of it directly.
                    relocations += get_code_and_relocations_from_elf(bin_name)

                # Keep track of changed bytes.
                if file_path not in diffs:
                    diffs[file_path] = OrderedDict()

                if org_offset in diffs[file_path]:
                    raise Exception(
                        "Duplicate .org directive within a single asm patch: %X."
                        % org_offset
                    )

                if is_custom_function and file_path == "main.dol":
                    objcopied_name = os.path.join(temp_dir, "main_copy.bin")
                    command = [
                        get_bin("powerpc-eabi-objcopy"),
                        "-O",
                        "binary",
                        bin_name,
                        objcopied_name,
                    ]
                    print(" ".join(command))
                    print()
                    result = call(command)
                    if result != 0:
                        raise Exception("Objcopy call failed.")
                    with open(objcopied_name, "rb") as f:
                        binary_data = f.read()
                else:
                    with open(bin_name, "rb") as f:
                        binary_data = f.read()

                code_chunk_size_in_bytes = len(binary_data)
                next_free_space_offsets[file_path] += code_chunk_size_in_bytes

                bytes = list(struct.unpack("B" * code_chunk_size_in_bytes, binary_data))
                diffs[file_path][org_offset] = OrderedDict()
                diffs[file_path][org_offset]["Data"] = bytes
                if relocations:
                    diffs[file_path][org_offset]["Relocations"] = relocations

        diff_path = os.path.join(".", "patch_diffs", "us", patch_name + "_diff.txt")
        with open(diff_path, "w") as f:
            f.write(
                yaml.dump(
                    diffs,
                    Dumper=yaml.CDumper,
                    default_flow_style=False,
                    line_break="\n",
                )
            )

    # Write the custom symbols to a text file.
    # Delete any entries in custom_symbols that have no custom symbols to avoid clutter.
    output_custom_symbols = OrderedDict()
    for file_path, custom_symbols_for_file in custom_symbols.items():
        if file_path != "main.dol" and len(custom_symbols_for_file) == 0:
            continue

        output_custom_symbols[file_path] = custom_symbols_for_file

    with open("./custom_symbols/us.txt", "w") as f:
        f.write(
            yaml.dump(
                output_custom_symbols,
                Dumper=yaml.CDumper,
                default_flow_style=False,
                line_break="\n",
            )
        )

    feature = "dynamic"
    if len(sys.argv) > 1 and sys.argv[1] == "debug":
        feature = "debug_dyn"

    # Build dynamic rust code (for a custom rel)
    if result := call(
        ["cargo", "build", "--features", feature, "--release"],
        cwd="./custom-functions",
    ):
        raise Exception("Building rust rel functions failed.")

    outpath = os.path.abspath(
        "./custom-functions/target/powerpc-unknown-eabi/release/libcustom_functions.a"
    )

    command = [
        get_bin("powerpc-eabi-ar"),
        "x",
        outpath,
    ]

    if result := call(command, cwd=temp_dir):
        raise Exception("Extracting objects from rust rel functions failed.")

    custom_elf = os.path.join(temp_dir, "dynamic-functions.o")

    command = [
        get_bin("powerpc-eabi-ld"),
        "-r",
        "-T",
        "merge.ld",
        "-o",
        custom_elf,
    ]

    for file in glob.glob(os.path.join(temp_dir, "*.o")):
        command.append(file)

    if result := call(command):
        raise Exception("Linker call failed.")

    create_lst("us", temp_dir)
    map_rel(
        os.path.join(temp_dir, "us_dyn.lst"),
        None,
        os.path.join(temp_dir, "us.lst"),
        0,
        [custom_elf],
    )
    with open(custom_elf, "rb") as elf_file, open(
        os.path.join(temp_dir, "us_dyn.lst")
    ) as sym:
        dat = elf_to_rel(1000, elf_file, sym)

    with open("../custom-rel/US/customNP.rel", "wb") as f:
        f.write(dat)

except Exception as e:
    stack_trace = traceback.format_exc()
    error_message = str(e) + "\n\n" + stack_trace
    print(error_message)
    input()
finally:
    shutil.rmtree(temp_dir)
