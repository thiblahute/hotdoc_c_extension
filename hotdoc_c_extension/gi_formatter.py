# -*- coding: utf-8 -*-
#
# Copyright © 2015,2016 Mathieu Duponchelle <mathieu.duponchelle@opencreed.com>
# Copyright © 2015,2016 Collabora Ltd
#
# This library is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library.  If not, see <http://www.gnu.org/licenses/>.

import os
import html
from wheezy.template.engine import Engine
from wheezy.template.ext.core import CoreExtension
from wheezy.template.ext.code import CodeExtension
from wheezy.template.loader import FileLoader
from hotdoc.core.formatter import Formatter
from hotdoc.core.symbols import *
import lxml.etree
from .fundamentals import FUNDAMENTALS
from hotdoc_c_extension.gi_flags import ReadableFlag, WritableFlag
from hotdoc_c_extension.gi_node_cache import ALL_GI_TYPES, is_introspectable
from hotdoc_c_extension.gi_symbols import GIClassSymbol, GIStructSymbol
from hotdoc_c_extension.gi_annotation_parser import GIAnnotationParser
from hotdoc_c_extension.gi_utils import Lang


class GIFormatter(Formatter):
    sitemap_language = None
    engine = None

    def __init__(self, gi_extension):
        Formatter.__init__(self, gi_extension)
        self._order_by_parent = True
        self._symbol_formatters.update(
                {GIClassSymbol: self._format_class_symbol,
                 GIStructSymbol: self._format_class_symbol})
        self._ordering.insert(self._ordering.index(ClassSymbol) + 1, GIClassSymbol)
        self._ordering.insert(self._ordering.index(GIClassSymbol) + 1, GIStructSymbol)
        self.__annotation_parser = GIAnnotationParser()

    def format_annotations (self, annotations):
        template = self.engine.get_template('gi_annotations.html')
        return template.render ({'annotations': annotations})

    def __add_attrs(self, symbol, **kwargs):
        if not symbol:
            return
        self.extension.add_attrs(symbol, **kwargs)
        for csym in symbol.get_children_symbols():
            self.__add_attrs(csym, **kwargs)

    def __wrap_in_language(self, symbol, langs_doc):
        template = self.get_template('symbol_language_wrapper.html')
        res = template.render( {'symbol': symbol, 'langs_doc': langs_doc})
        return res

    def _format_symbol(self, symbol):
        if isinstance(symbol, (QualifiedSymbol, FieldSymbol, EnumMemberSymbol)):
            return Formatter._format_symbol(self, symbol)

        langs_doc = {}
        prev_lang = None
        for lang in self.extension.languages:
            langs_doc[lang] = None
            if is_introspectable(symbol.unique_name, lang):
                self.extension.setup_language(lang, prev_lang)
                self.__add_attrs(symbol, language=lang)
                langs_doc[lang] = Formatter._format_symbol(self, symbol)
            prev_lang = lang

        self.extension.setup_language(None, prev_lang)
        return self.__wrap_in_language(symbol, langs_doc)

    def _format_flags (self, flags):
        template = self.engine.get_template('gi_flags.html')
        out = template.render ({'flags': flags})
        return out

    def _format_type_tokens(self, symbol, type_tokens):
        language = symbol.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.c:
            return Formatter._format_type_tokens (self, symbol, type_tokens)

        type_desc = self.extension.get_attr(symbol, 'type_desc')
        assert(type_desc)
        gi_name = type_desc.gi_name
        new_tokens = []
        link = None
        if gi_name in FUNDAMENTALS[language]:
            fund_link = FUNDAMENTALS[language][gi_name]
            link = Link(fund_link.ref, fund_link._title, gi_name)
        elif gi_name in ALL_GI_TYPES:
            ctype_name = ALL_GI_TYPES[gi_name]
            link = self.extension.app.link_resolver.get_named_link(ctype_name)

        if language == Lang.cs:
            if type_desc.is_out:
                new_tokens += [FUNDAMENTALS[Lang.cs]['out'], ' ']
            if not link:
                link = self.extension.get_attr(symbol, 'cs_link')

            if link:
                new_tokens.append(link)
            else: # Should not happen but let's be conservative
                new_tokens.append(type_desc.gi_name)

            if type_desc.nesting_depth:
                new_tokens.append('[]')
        else:
            if type_desc.nesting_depth:
                new_tokens.append('[' * type_desc.nesting_depth + ' ')
            if link:
                new_tokens.append(link)
            else: # Should not happen but let's be conservative
                new_tokens.append(type_desc.gi_name)
            if type_desc.nesting_depth:
                new_tokens.append(']' * type_desc.nesting_depth)

        return Formatter._format_type_tokens (self, symbol, new_tokens)

    def __add_annotations (self, symbol):
        if self.extension.get_attr(symbol, 'language') == Lang.c:
            annotations = self.__annotation_parser.make_annotations(symbol)

            # FIXME: OK this is format time but still seems strange
            if annotations:
                extra_content = self.format_annotations (annotations)
                symbol.extension_contents['Annotations'] = extra_content
        else:
            symbol.extension_contents.pop('Annotations', None)

    def _format_return_item_symbol(self, symbol):
        self.__add_annotations(symbol)
        return Formatter._format_return_item_symbol (self, symbol)

    def _format_return_value_symbol (self, *args):
        retval = args[0]
        is_void = retval[0] is None or \
                retval[0].get_extension_attribute('gi-extension',
                        'gi_name') == 'none'

        if not is_void:
            language = retval[0].get_extension_attribute(self.extension.extension_name, 'language')
        else:
            language = Lang.c

        if language in [Lang.c, Lang.cs]:
            if is_void:
                retval = [None]
            else:
                retval = retval[:1]

            for item in retval:
                if item:
                    item.formatted_link = ''

        elif is_void:
            retval = retval[1:] or [None]

        args = list(args)
        args[0] = retval
        return Formatter._format_return_value_symbol (self, *args)

    def _format_parameter_symbol (self, parameter):
        language = parameter.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.cs and self.extension.get_attr(parameter, 'instance_param'):
            return None

        self.__add_annotations(parameter)
        direction = self.extension.get_attr(parameter, 'direction')
        if language in [Lang.py, Lang.js]:
            if direction == 'out':
                return None

            parameter.extension_contents['type-link'] = self._format_linked_symbol (parameter)
        else:
            parameter.extension_contents.pop('type-link', None)

        res = Formatter._format_parameter_symbol (self, parameter)
        return res

    def _format_linked_symbol (self, symbol):
        if not symbol:
            return Formatter._format_linked_symbol (self, symbol)

        language = symbol.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.c:
            res = Formatter._format_linked_symbol (self, symbol)
            if symbol == None:
                res = 'void'
            return res

        if not isinstance (symbol, QualifiedSymbol):
            return Formatter._format_linked_symbol (self, symbol)

        type_desc = symbol.get_extension_attribute ('gi-extension', 'type_desc')
        if type_desc:
            return self._format_type_tokens (symbol, symbol.type_tokens)

        return Formatter._format_linked_symbol (self, symbol)

    def _format_prototype (self, function, is_pointer, title):
        language = function.get_extension_attribute(self.extension.extension_name, 'language')

        if language == Lang.c:
            return Formatter._format_prototype (self, function,
                    is_pointer, title)

        params = function.get_extension_attribute ('gi-extension', 'parameters')

        if language == Lang.cs:
            params = function.parameters

            function.parameters = [p for p in params if not self.extension.get_attr(p, 'instance_param')]
            res = Formatter._format_prototype (self, function, is_pointer, title)
            function.parameters = params

            return res

        if params is None:
            return Formatter._format_prototype (self, function,
                    is_pointer, title)

        c_name = function._make_name()

        if language == Lang.py:
            template = self.engine.get_template('python_prototype.html')
        elif language == Lang.js:
            template = self.engine.get_template('javascript_prototype.html')

        if type (function) == SignalSymbol:
            comment = "%s callback for the '%s' signal" % (language, c_name)
        elif type (function) == VFunctionSymbol:
            comment = "%s implementation of the '%s' virtual method" % \
                    (language, c_name)
        else:
            comment = "%s wrapper for '%s'" % (language,
                    c_name)

        res = template.render ({'return_value': function.return_value,
            'parent_name': function.parent_name, 'function_name': title, 'parameters':
            params, 'comment': comment, 'throws': function.throws,
            'out_params': [], 'is_method': isinstance(function, MethodSymbol)})

        return res

    def _format_vfunction_symbol (self, vmethod):
        title = vmethod.link.title
        language = vmethod.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.py:
            vmethod.link.title = 'do_%s' % vmethod._make_name()
            title = 'do_%s' % title
        elif language == Lang.js:
            vmethod.link.title = '%s::%s' % (vmethod.parent_name, vmethod._make_name())
            title = 'vfunc_%s' % title
        return Formatter._format_vfunction_symbol (self, vmethod)

    def _format_members_list (self, members, member_designation, struct):
        language = struct.get_extension_attribute(self.extension.extension_name, 'language')

        if language == Lang.cs:
            # Those are properties in c#
            return ''

        if language != Lang.c:
            # Never render members that are in a union, introspected won't show them
            members = [m for m in members if not m.get_extension_attribute(
                self.extension.extension_name, 'in_union')]

        return super()._format_members_list (members, member_designation, struct)

    def _format_struct (self, struct):
        language = struct.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.c:
            return Formatter._format_struct (self, struct)

        if language == Lang.cs:
            struct.hierarchy = []
            struct.children = {}
            struct.interfaces = []
            return Formatter._format_class_symbol( self, struct)

        members_list = self._format_members_list (struct.members, 'Attributes', struct)

        template = self.engine.get_template ("python_compound.html")
        out = template.render ({"symbol": struct,
                                "members_list": members_list})
        return out

    def _format_interfaces(self, klass, interfaces):
        language = klass.get_extension_attribute(self.extension.extension_name, 'language')
        interfaces = self.extension.get_attr(klass, language + '_interfaces', klass.interfaces)

        return super()._format_interfaces(klass, interfaces)

    def _format_class_symbol (self, klass):
        saved_raw_text = klass.raw_text
        if klass.get_extension_attribute(self.extension.extension_name, 'language') != Lang.c:
            klass.raw_text = None
        out = Formatter._format_class_symbol(self, klass)

        if klass.get_extension_attribute(self.extension.extension_name, 'language') == Lang.c:
            # Render class structure if available.
            if klass.class_struct_symbol:
                out += '<h3>Class structure</h3>'
                out += klass.class_struct_symbol.detailed_description

        klass.raw_text = saved_raw_text
        return out

    def _format_constant(self, constant):
        language = constant.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.c:
            return Formatter._format_constant (self, constant)

        template = self.engine.get_template('constant.html')
        out = template.render ({'symbol': constant,
                                'definition': None,
                                'constant': constant})
        return out

    def _format_comment(self, comment, link_resolver):
        ast = comment.extension_attrs['gi-extension']['ast']

        if not comment.description:
            out = u''
        elif ast:
            out = self._docstring_formatter.ast_to_html(
                ast, link_resolver)
        else:
            ast = self._docstring_formatter.comment_to_ast(
                comment, link_resolver)
            out = self._docstring_formatter.ast_to_html(
                ast, link_resolver)
            comment.extension_attrs['gi-extension']['ast'] = ast

        return out

    def _format_callable(self, callable_, callable_type, title,
                         is_pointer=False):
        language = callable_.get_extension_attribute(self.extension.extension_name, 'language')

        if language == Lang.cs:
            if self.extension.get_attr(callable_, 'csharp_prop'):
                return ''
        elif language == Lang.py and isinstance(callable_, ClassMethodSymbol):
            return None

        return super()._format_callable(callable_, callable_type, title, is_pointer)

    def _format_property_prototype(self, prop):
        language = prop.get_extension_attribute(self.extension.extension_name, 'language')
        if language != Lang.cs:
            return super()._format_property_prototype(prop)

        type_link = self._format_linked_symbol(prop.prop_type)
        template = self.get_template('cs_property_prototype.html')
        flags = self.extension.get_attr(prop, 'flags')
        getset = ''
        for flag in flags:
            if isinstance(flag, ReadableFlag):
                getset += 'get; '
            elif isinstance(flag, WritableFlag):
                getset += 'set; '
        return template.render({'property_name': prop.link.title,
                                'property_type': type_link,
                                'getset': getset})

    def _format_property_symbol(self, prop):
        language = prop.get_extension_attribute(self.extension.extension_name, 'language')

        if language != Lang.cs and self.extension.get_attr(prop, 'csharp_prop'):
            return ''

        flags = self.extension.get_attr(prop, 'flags')
        extra_content = self._format_flags (flags)
        prop.extension_contents['Flags'] = extra_content

        if language == Lang.py:
            prop.link.title = 'self.props.%s' % prop.display_name.replace('-', '_')

        return super()._format_property_symbol(prop)

    def _format_alias(self, alias):
        language = alias.get_extension_attribute(self.extension.extension_name, 'language')
        if language == Lang.c:
            return super()._format_alias(alias)

        return None

    def get_template(self, name):
        return GIFormatter.engine.get_template(name)

    def parse_toplevel_config(self, config):
        super().parse_toplevel_config(config)
        if GIFormatter.engine is None:
            module_path = os.path.dirname(__file__)
            searchpath = [os.path.join(module_path, "templates")] + Formatter.engine.loader.searchpath
            GIFormatter.engine = Engine(
                    loader=FileLoader(searchpath, encoding='UTF-8'),
                    extensions=[CoreExtension(), CodeExtension()])
            GIFormatter.engine.global_vars.update({'e': html.escape})
