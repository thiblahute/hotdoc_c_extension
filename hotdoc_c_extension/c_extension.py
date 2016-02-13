import os, sys, linecache, pkgconfig, glob, subprocess

import clang.cindex
from ctypes import *
from fnmatch import fnmatch

from hotdoc.core import file_includer
from hotdoc.core.base_extension import BaseExtension
from hotdoc.core.exceptions import ParsingException, BadInclusionException
from hotdoc.core.symbols import *
from hotdoc.core.comment_block import comment_from_tag
from hotdoc.core.links import Link
from hotdoc.core.wizard import HotdocWizard

from hotdoc.parsers.gtk_doc_parser import GtkDocParser

from hotdoc.utils.loggable import (info as core_info, warn, Logger,
    debug as core_debug)
from hotdoc.utils.wizard import Skip, QuickStartWizard

from .c_comment_scanner.c_comment_scanner import get_comments

def ast_node_is_function_pointer (ast_node):
    if ast_node.kind == clang.cindex.TypeKind.POINTER and \
            ast_node.get_pointee().get_result().kind != \
            clang.cindex.TypeKind.INVALID:
        return True
    return False


def info(message):
    core_info(message, domain='c-extension')


def debug(message):
    core_debug(message, domain='c-extension')


Logger.register_warning_code('clang-diagnostic', ParsingException,
                             'c-extension')
Logger.register_warning_code('clang-heisenbug', ParsingException,
                             'c-extension')
Logger.register_warning_code('clang-flags', ParsingException,
                             'c-extension')
Logger.register_warning_code('bad-c-inclusion', BadInclusionException,
                             'c-extension')


def get_clang_headers():
    version = subprocess.check_output(['llvm-config', '--version']).strip()
    prefix = subprocess.check_output(['llvm-config', '--prefix']).strip()

    return os.path.join(prefix, 'lib', 'clang', version, 'include')

def get_clang_libdir():
    return subprocess.check_output(['llvm-config', '--libdir']).strip()

class ClangScanner(object):
    def __init__(self, doc_repo, doc_db):
        if not clang.cindex.Config.loaded:
            # Let's try and find clang ourselves first
            clang_libdir = get_clang_libdir()
            if os.path.exists(clang_libdir):
                clang.cindex.Config.set_library_path(clang_libdir)

        self.__raw_comment_parser = GtkDocParser(doc_repo)
        self.doc_repo = doc_repo
        self.__doc_db = doc_db

    def scan(self, filenames, options, incremental, full_scan,
             full_scan_patterns, fail_fast=False):
        index = clang.cindex.Index.create()
        flags = clang.cindex.TranslationUnit.PARSE_INCOMPLETE |\
                clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD

        info('scanning %d C source files' % len(filenames))
        self.filenames = filenames

        # FIXME: er maybe don't do that ?
        args = ["-Wno-attributes"]
        args.append ("-isystem%s" % get_clang_headers())
        args.extend (options)
        self.symbols = {}
        self.parsed = set({})

        debug('CFLAGS %s' % ' '.join(args))

        if not full_scan:
            for filename in filenames:
                with open (filename, 'r') as f:
                    debug('Getting comments in %s' % filename)
                    cs = get_comments (filename)
                    for c in cs:
                        block = self.__raw_comment_parser.parse_comment(c[0],
                                c[1], c[2], c[3], self.doc_repo.include_paths)
                        if block is not None:
                            self.doc_repo.doc_database.add_comment(block)

        for filename in self.filenames:
            if filename in self.parsed:
                continue

            do_full_scan = any(fnmatch(filename, p) for p in full_scan_patterns)
            if do_full_scan:
                debug('scanning %s' % filename)
                tu = index.parse(filename, args=args, options=flags)

                for diag in tu.diagnostics:
                    warn('clang-diagnostic', 'Clang issue : %s' % str(diag))

                self.__parse_file (filename, tu, full_scan)
                for include in tu.get_includes():
                    self.__parse_file (os.path.abspath(str(include.include)),
                                       tu, full_scan)
        return True

    def __parse_file (self, filename, tu, full_scan):
        if filename in self.parsed:
            return

        self.parsed.add (filename)
        start = tu.get_location (filename, 0)
        end = tu.get_location (filename, int(os.path.getsize(filename)))
        extent = clang.cindex.SourceRange.from_locations (start, end)
        cursors = self.__get_cursors(tu, extent)
        if filename in self.filenames:
            self.__create_symbols (cursors, tu, full_scan)

    # That's the fastest way of obtaining our ast nodes for a given filename
    def __get_cursors (self, tu, extent):
        tokens_memory = POINTER(clang.cindex.Token)()
        tokens_count = c_uint()

        clang.cindex.conf.lib.clang_tokenize(tu, extent, byref(tokens_memory),
                byref(tokens_count))

        count = int(tokens_count.value)

        if count < 1:
            return

        cursors = (clang.cindex.Cursor * count)()
        clang.cindex.conf.lib.clang_annotateTokens (tu, tokens_memory, tokens_count,
                cursors)

        return cursors

    def __create_symbols(self, nodes, tu, full_scan):
        for node in nodes:
            node._tu = tu

            # This is dubious, needed to parse G_DECLARE_FINAL_TYPE
            # investigate further (fortunately this doesn't seem to
            # significantly impact performance ( ~ 5% )
            if node.kind == clang.cindex.CursorKind.TYPE_REF:
                node = node.get_definition()
                if not node:
                    continue

                if not unicode(node.location.file) in self.filenames:
                    continue

            # if node.kind in [clang.cindex.CursorKind.FUNCTION_DECL,
            #                 clang.cindex.CursorKind.TYPEDEF_DECL,
            #                 clang.cindex.CursorKind.MACRO_DEFINITION,
            #                 clang.cindex.CursorKind.VAR_DECL]:
            #     if full_scan:
            #         if not node.raw_comment:
            #             continue
            #         block = self.__raw_comment_parser.parse_comment (
            #             node.raw_comment, str(node.location.file), 0, 0,
            #             self.doc_repo.include_paths)
            #         self.doc_repo.add_comment(block)

            if node.spelling in self.symbols:
                continue

            sym = None
            func_dec = self.__getFunctionDeclNode(node)
            if func_dec:
                sym = self.__create_function_symbol(func_dec)
            elif node.kind == clang.cindex.CursorKind.VAR_DECL:
                sym = self.__create_exported_variable_symbol (node)
            elif node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
                sym = self.__create_macro_symbol (node)
            elif node.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
                sym = self.__create_typedef_symbol (node)

            if sym is not None:
                self.symbols[node.spelling] = sym

    def __getFunctionDeclNode(self, node):
        if not node.location.file:
            return None
        elif node.location.file.name.endswith(".h"):
            if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
                return node
            else:
                return None

        if node.kind != clang.cindex.CursorKind.COMPOUND_STMT:
            return None

        if node.semantic_parent.kind == clang.cindex.CursorKind.FUNCTION_DECL:
            return node.semantic_parent

        return None

    def __apply_qualifiers (self, type_, tokens):
        if type_.is_const_qualified():
            tokens.append ('const ')
        if type_.is_restrict_qualified():
            tokens.append ('restrict ')
        if type_.is_volatile_qualified():
            tokens.append ('volatile ')

    def make_c_style_type_name (self, type_):
        tokens = []
        while (type_.kind == clang.cindex.TypeKind.POINTER):
            self.__apply_qualifiers(type_, tokens)
            tokens.append ('*')
            type_ = type_.get_pointee()

        if type_.kind == clang.cindex.TypeKind.TYPEDEF:
            d = type_.get_declaration ()
            link = Link (None, d.displayname, d.displayname)

            tokens.append (link)
            self.__apply_qualifiers(type_, tokens)
        else:
            tokens.append (type_.spelling + ' ')

        tokens.reverse()
        return tokens

    def __create_callback_symbol (self, node, comment):
        parameters = []

        if comment:
            return_tag = comment.tags.get('returns')
            return_comment = comment_from_tag(return_tag)
        else:
            return_comment = None

        return_value = None

        for child in node.get_children():
            if not return_value:
                t = node.underlying_typedef_type
                res = t.get_pointee().get_result()
                type_tokens = self.make_c_style_type_name (res)
                return_value = [ReturnItemSymbol(type_tokens=type_tokens,
                        comment=return_comment)]
            else:
                if comment:
                    param_comment = comment.params.get (child.displayname)
                else:
                    param_comment = None

                type_tokens = self.make_c_style_type_name (child.type)
                parameter = ParameterSymbol (argname=child.displayname,
                        type_tokens=type_tokens, comment=param_comment)
                parameters.append (parameter)

        if not return_value:
            return_value = [ReturnItemSymbol(type_tokens=[], comment=None)]

        sym = self.__doc_db.get_or_create_symbol(CallbackSymbol, parameters=parameters,
                return_value=return_value, comment=comment, display_name=node.spelling,
                filename=str(node.location.file), lineno=node.location.line)
        return sym

    def __parse_public_fields (self, decl):
        tokens = decl.translation_unit.get_tokens(extent=decl.extent)
        delimiters = []

        filename = str(decl.location.file)

        start = decl.extent.start.line
        end = decl.extent.end.line + 1
        original_lines = [linecache.getline(filename, i).rstrip() for i in range(start,
            end)]

        public = True
        if (self.__locate_delimiters(tokens, delimiters)):
            public = False

        children = []
        for child in decl.get_children():
            children.append(child)

        delimiters.reverse()
        if not delimiters:
            return '\n'.join (original_lines), children

        public_children = []
        children = []
        for child in decl.get_children():
            children.append(child)
        children.reverse()
        if children:
            next_child = children.pop()
        else:
            next_child = None
        next_delimiter = delimiters.pop()

        final_text = []

        found_first_child = False

        for i, line in enumerate(original_lines):
            lineno = i + start
            if next_delimiter and lineno == next_delimiter[1]:
                public = next_delimiter[0]
                if delimiters:
                    next_delimiter = delimiters.pop()
                else:
                    next_delimiter = None
                continue

            if not next_child or lineno < next_child.location.line:
                if public or not found_first_child:
                    final_text.append (line)
                continue

            if lineno == next_child.location.line:
                found_first_child = True
                if public:
                    final_text.append (line)
                    public_children.append (next_child)
                while next_child.location.line == lineno:
                    if not children:
                        public = True
                        next_child = None
                        break
                    next_child = children.pop()

        return ('\n'.join(final_text), public_children)

    def __locate_delimiters (self, tokens, delimiters):
        public_pattern = "/*<public>*/"
        private_pattern = "/*<private>*/"
        protected_pattern = "/*<protected>*/"
        had_public = False
        for tok in tokens:
            if tok.kind == clang.cindex.TokenKind.COMMENT:
                comment = ''.join(tok.spelling.split())
                if public_pattern == comment:
                    had_public = True
                    delimiters.append((True, tok.location.line))
                elif private_pattern == comment:
                    delimiters.append((False, tok.location.line))
                elif protected_pattern == comment:
                    delimiters.append((False, tok.location.line))
        return had_public

    def __create_struct_symbol (self, node, comment):
        underlying = node.underlying_typedef_type
        decl = underlying.get_declaration()
        raw_text, public_fields = self.__parse_public_fields (decl)
        members = []
        for field in public_fields:
            if comment:
                member_comment = comment.params.get (field.spelling)
            else:
                member_comment = None

            type_tokens = self.make_c_style_type_name (field.type)
            is_function_pointer = ast_node_is_function_pointer (field.type)
            member = FieldSymbol (is_function_pointer=is_function_pointer,
                    member_name=field.spelling, type_tokens=type_tokens,
                    comment=member_comment)
            members.append (member)

        if not public_fields:
            raw_text = None

        return self.__doc_db.get_or_create_symbol(StructSymbol, raw_text=raw_text, members=members,
                comment=comment, display_name=node.spelling,
                filename=str(decl.location.file), lineno=decl.location.line)

    def __create_enum_symbol (self, node, comment):
        members = []
        underlying = node.underlying_typedef_type
        decl = underlying.get_declaration()
        for member in decl.get_children():
            if comment:
                member_comment = comment.params.get (member.spelling)
            else:
                member_comment = None
            member_value = member.enum_value
            # FIXME: this is pretty much a macro symbol ?
            member = self.__doc_db.get_or_create_symbol(Symbol, comment=member_comment, display_name=member.spelling,
                    filename=str(member.location.file),
                    lineno=member.location.line)
            member.enum_value = member_value
            members.append (member)

        return self.__doc_db.get_or_create_symbol(EnumSymbol, members=members, comment=comment, display_name=node.spelling,
                filename=str(decl.location.file), lineno=decl.location.line)

    def __create_alias_symbol (self, node, comment):
        type_tokens = self.make_c_style_type_name(node.underlying_typedef_type)
        aliased_type = QualifiedSymbol (type_tokens=type_tokens)
        return self.__doc_db.get_or_create_symbol(AliasSymbol, aliased_type=aliased_type, comment=comment,
                display_name=node.spelling, filename=str(node.location.file),
                lineno=node.location.line)

    def __create_typedef_symbol (self, node):
        t = node.underlying_typedef_type
        comment = self.doc_repo.doc_database.get_comment (node.spelling)
        if ast_node_is_function_pointer (t):
            sym = self.__create_callback_symbol (node, comment)
        else:
            d = t.get_declaration()
            if d.kind == clang.cindex.CursorKind.STRUCT_DECL:
                sym = self.__create_struct_symbol (node, comment)
            elif d.kind == clang.cindex.CursorKind.ENUM_DECL:
                sym = self.__create_enum_symbol (node, comment)
            else:
                sym = self.__create_alias_symbol (node, comment)
        return sym

    def __create_function_macro_symbol (self, node, comment, original_text):
        return_value = [None]
        if comment:
            return_tag = comment.tags.get ('returns')
            if return_tag:
                return_comment = comment_from_tag (return_tag)
                return_value = [ReturnItemSymbol (comment=return_comment)]

        parameters = []
        if comment:
            for param_name, param_comment in comment.params.iteritems():
                parameter = ParameterSymbol (argname=param_name, comment = param_comment)
                parameters.append (parameter)

        sym = self.__doc_db.get_or_create_symbol(FunctionMacroSymbol, return_value=return_value,
                parameters=parameters, original_text=original_text,
                comment=comment, display_name=node.spelling,
                filename=str(node.location.file), lineno=node.location.line)
        return sym

    def __create_constant_symbol (self, node, comment, original_text):
        return self.__doc_db.get_or_create_symbol(ConstantSymbol, original_text=original_text, comment=comment,
                display_name=node.spelling, filename=str(node.location.file),
                lineno=node.location.line)

    def __create_macro_symbol (self, node):
        l = linecache.getline (str(node.location.file), node.location.line)
        split = l.split()

        if len (split) < 2:
            warn("clang-heisenbug",
                    "Found a strange #define, please report the following:\n" +
                 l + str(node.location))
            return None

        start = node.extent.start.line
        end = node.extent.end.line + 1
        filename = str(node.location.file)
        original_lines = [linecache.getline(filename, i).rstrip() for i in range(start,
            end)]
        original_text = '\n'.join(original_lines)
        comment = self.doc_repo.doc_database.get_comment (node.spelling)
        if '(' in split[1]:
            sym = self.__create_function_macro_symbol (node, comment, original_text)
        else:
            sym = self.__create_constant_symbol (node, comment, original_text)

        return sym

    def __create_function_symbol (self, node):
        comment = self.doc_repo.doc_database.get_comment (node.spelling)
        parameters = []

        if comment:
            return_tag = comment.tags.get('returns')
            return_comment = comment_from_tag(return_tag)
        else:
            return_comment = None

        type_tokens = self.make_c_style_type_name (node.result_type)
        return_value = [ReturnItemSymbol (type_tokens=type_tokens,
                comment=return_comment)]

        for param in node.get_arguments():
            if comment:
                param_comment = comment.params.get (param.displayname)
            else:
                param_comment = None

            type_tokens = self.make_c_style_type_name (param.type)
            parameter = ParameterSymbol (argname=param.displayname,
                    type_tokens=type_tokens, comment=param_comment)
            parameters.append (parameter)

        sym = self.__doc_db.get_or_create_symbol(FunctionSymbol, parameters=parameters,
                return_value=return_value, comment=comment, display_name=node.spelling,
                filename=str(node.location.file), lineno=node.location.line,
                extent_start=node.extent.start.line,
                extent_end=node.extent.end.line)

        return sym

    def __create_exported_variable_symbol (self, node):
        l = linecache.getline (str(node.location.file), node.location.line)
        split = l.split()

        start = node.extent.start.line
        end = node.extent.end.line + 1
        filename = str(node.location.file)
        original_lines = [linecache.getline(filename, i).rstrip() for i in range(start,
            end)]
        original_text = '\n'.join(original_lines)
        comment = self.doc_repo.doc_database.get_comment (node.spelling)

        type_tokens = self.make_c_style_type_name(node.type)
        type_qs = QualifiedSymbol(type_tokens=type_tokens)

        sym = self.__doc_db.get_or_create_symbol(ExportedVariableSymbol, original_text=original_text,
                comment=self.doc_repo.doc_database.get_comment (node.spelling),
                display_name=node.spelling, filename=str(node.location.file),
                lineno=node.location.line, type_qs=type_qs)
        return sym

PKG_PROMPT=\
"""
The C extension uses clang to parse the source code
of your project, and discover the symbols exposed.

It thus needs to be provided with the set of flags
used to compile the library.

In this stage of the configuration, you will first be
prompted for the package names the library depends
upon, as you would pass them to pkg-config, for
example if the library depends on the glib, the call
to pkg-config would be:

$ pkg-config glib-2.0 --cflags

so you would want to return:

>>> ['glib-2.0']

here.

You will then be prompted for any additional flags needed
for the compilation, for example:

>>> ["-Dfoo=bar"]

After the flags have been confirmed, the extension will
try to compile your library. If clang can't compile
without warnings, compilation will stop, the batch
of warnings will be displayed, and you'll be given the chance
to correct the flags you passed.
"""

C_SOURCES_PROMPT=\
"""
Please pass a list of c source files (headers and implementation).

You can pass wildcards here, for example:

>>> ['../foo/*.c', '../foo/*.h']

These wildcards will be evaluated each time hotdoc is run.

You will be prompted for source files to ignore afterwards.
"""

C_FILTERS_PROMPT=\
"""
Please pass a list of c source files to ignore.

You can pass wildcards here, for example:

>>> ['../foo/*priv*']

These wildcards will be evaluated each time hotdoc is run.
"""

def validate_pkg_config_packages(wizard, packages):
    if packages is None:
        return True

    if type(packages) != list:
        warn('clang-flags', "Incorrect type, expected list or None")
        return False

    for package in packages:
        if not pkgconfig.exists(package):
            warn('clang-flags', 'package %s does not exist' % package)
            return False

    return True

def source_files_from_config(config, conf_path_resolver):
    sources = resolve_patterns(config.get('c_sources', []), conf_path_resolver)
    filters = resolve_patterns(config.get('c_source_filters', []),
            conf_path_resolver)
    sources = [item for item in sources if item not in filters]
    return [os.path.abspath(source) for source in sources]

def flags_from_config(config, path_resolver):
    flags = []

    for package in config.get('pkg_config_packages') or []:
        flags.extend(pkgconfig.cflags(package).split(' '))

    extra_flags = config.get('extra_c_flags') or []

    for extra_flag in extra_flags:
        extra_flag = extra_flag.strip()
        if extra_flag.startswith('-I'):
            path = extra_flag.split('-I')[1]
            flags.append('-I%s' % path_resolver.resolve_config_path(path))
        else:
            flags.append(extra_flag)

    return flags

def resolve_patterns(source_patterns, conf_path_resolver):
    if source_patterns is None:
        return []

    source_files = []
    for item in source_patterns:
        item = conf_path_resolver.resolve_config_path(item)
        source_files.extend(glob.glob(item))

    return source_files

def validate_filters(wizard, thing):
    if thing is None:
        return True

    if not QuickStartWizard.validate_globs_list(wizard, thing):
        return False

    source_files = resolve_patterns(wizard.config.get('c_sources', []), wizard)

    filters = resolve_patterns(thing, wizard)

    source_files = [item for item in source_files if item not in filters]

    print "The files to be parsed would now be %s" % source_files

    return wizard.ask_confirmation()

DESCRIPTION =\
"""
Parse C source files to extract comments and symbols.
"""


class CExtension(BaseExtension):
    EXTENSION_NAME = 'c-extension'
    flags = None
    sources = None

    def __init__(self, doc_repo):
        BaseExtension.__init__(self, doc_repo)
        self.doc_repo = doc_repo
        file_includer.include_signal.connect(self.__include_file_cb)

    # pylint: disable=no-self-use
    def __include_file_cb(self, include_path, line_ranges, symbol_name):
        if not include_path.endswith(".c") or not symbol_name:
            return None

        if not line_ranges:
            line_ranges = [(1, -1)]
        symbol = self.doc_repo.doc_database.get_symbol(symbol_name)
        if symbol and symbol.filename != include_path:
            symbol = None

        if not symbol:
            scanner = ClangScanner(self.doc_repo, self)
            scanner.scan([include_path], CExtension.flags,
                         self.doc_repo.incremental, True, ['*.c', '*.h'])
            symbol = self.doc_repo.doc_database.get_symbol(symbol_name)

            if not symbol:
                warn('bad-c-inclusion',
                     "Trying to include symbol %s but could not be found in "
                     "%s" % (symbol_name, include_path))
                return None

        res = ''
        for n, (start, end) in enumerate(line_ranges):
            if n != 0:
                res += "\n...\n"

            start += symbol.extent_start - 2
            if end > 0:
                end += (symbol.extent_start - 1)  # We are inclusive here
            else:
                end = symbol.extent_end

            with open(include_path, "r") as _:
                res += "\n".join(_.read().split("\n")[start:end])

        if res:
            return res, 'c'

        return None

    def setup(self):
        stale, unlisted = self.get_stale_files(CExtension.sources)
        self.scanner = ClangScanner(self.doc_repo, self)
        self.scanner.scan(stale, CExtension.flags,
                          self.doc_repo.incremental, False, ['*.h'])

    @staticmethod
    def validate_c_extension(wizard):
        sources = source_files_from_config(wizard.config, wizard)

        if not sources:
            return

        flags = flags_from_config(wizard.config, wizard)

        print "scanning C sources"
        wizard.include_paths = []
        scanner = ClangScanner(wizard, wizard)

        if not scanner.scan(sources, flags, False, False, ['*.h'], fail_fast=True):
            if not wizard.ask_confirmation("Scanning failed, try again [y,n]? "):
                raise Skip
            return False
        return True

    @staticmethod
    def add_arguments (parser):
        group = parser.add_argument_group('C extension', DESCRIPTION,
                validate_function=CExtension.validate_c_extension)
        group.add_argument ("--c-sources", action="store", nargs="+",
                dest="c_sources", help="C source files to parse",
                extra_prompt=C_SOURCES_PROMPT,
                validate_function=QuickStartWizard.validate_globs_list,
                finalize_function=HotdocWizard.finalize_paths)
        group.add_argument ("--c-source-filters", action="store", nargs="+",
                dest="c_source_filters", help="C source files to ignore",
                extra_prompt=C_FILTERS_PROMPT,
                validate_function=validate_filters,
                finalize_function=HotdocWizard.finalize_paths)
        group.add_argument ("--pkg-config-packages", action="store", nargs="+",
                dest="pkg_config_packages", help="Packages the library depends upon",
                extra_prompt=PKG_PROMPT,
                validate_function=validate_pkg_config_packages)
        group.add_argument ("--extra-c-flags", action="store", nargs="+",
                dest="extra_c_flags", help="Extra C flags (-D, -I)",
                validate_function=QuickStartWizard.validate_list)

    @staticmethod
    def parse_config(doc_repo, config):
        CExtension.flags = flags_from_config(config, doc_repo)
        sources = source_files_from_config(config, doc_repo)
        CExtension.sources = [os.path.abspath(filename) for filename in
                sources]


def get_extension_classes():
    return [CExtension]
