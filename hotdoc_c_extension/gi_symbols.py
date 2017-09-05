# -*- coding: utf-8 -*-
#
# Copyright © 2017 Thibault Saunier <tsaunier@gnome.org>
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

from hotdoc.core.symbols import *
from hotdoc_c_extension.gi_utils import Lang

class GIClassSymbol(ClassSymbol):
    def __init__(self, **kwargs):
        self.class_struct_symbol = None
        ClassSymbol.__init__(self, **kwargs)

    def get_children_symbols(self):
        extra_children = [self.class_struct_symbol]
        for lang in Lang.all():
            extra_children += self.get_extension_attribute('gi-extension',
                                                   lang + '_interfaces', [])
        return extra_children + super().get_children_symbols()

class GIStructSymbol(ClassSymbol):
    """Boxed types are pretty much handled like classes with a possible
       hierarchy, constructors, methods, and in csharp they can even implement
       interfaces"""
    __tablename__ = 'structures'

    def __init__(self, **kwargs):
        self.class_struct_symbol = None
        ClassSymbol.__init__(self, **kwargs)

    def get_children_symbols(self):
        extra_children = []
        for lang in Lang.all():
            extra_children += self.get_extension_attribute('gi-extension',
                                                   lang + '_interfaces', [])
        return extra_children + super().get_children_symbols()
