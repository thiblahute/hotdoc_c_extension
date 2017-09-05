import os
from collections import defaultdict
from lxml import etree
import networkx as nx
from hotdoc.core.symbols import QualifiedSymbol
from hotdoc_c_extension.gi_utils import *
from hotdoc_c_extension.fundamentals import FUNDAMENTALS


'''
Names of boilerplate GObject macros we don't want to expose
'''
SMART_FILTERS = set()


def __generate_smart_filters(id_prefixes, sym_prefixes, node):
    sym_prefix = node.attrib['{%s}symbol-prefix' % NS_MAP[Lang.c]]
    SMART_FILTERS.add(('%s_IS_%s' % (sym_prefixes, sym_prefix)).upper())
    SMART_FILTERS.add(('%s_TYPE_%s' % (sym_prefixes, sym_prefix)).upper())
    SMART_FILTERS.add(('%s_%s' % (sym_prefixes, sym_prefix)).upper())
    SMART_FILTERS.add(('%s_%s_CLASS' % (sym_prefixes, sym_prefix)).upper())
    SMART_FILTERS.add(('%s_IS_%s_CLASS' % (sym_prefixes, sym_prefix)).upper())
    SMART_FILTERS.add(('%s_%s_GET_CLASS' % (sym_prefixes, sym_prefix)).upper())
    SMART_FILTERS.add(('%s_%s_GET_IFACE' % (sym_prefixes, sym_prefix)).upper())


__HIERARCHY_GRAPH = nx.DiGraph()


ALL_GI_TYPES = {}


# Avoid parsing gir files multiple times
__PARSED_GIRS = set()


def __find_gir_file(gir_name, all_girs):
    if gir_name in all_girs:
        return all_girs[gir_name]

    xdg_dirs = os.getenv('XDG_DATA_DIRS') or ''
    xdg_dirs = [p for p in xdg_dirs.split(':') if p]
    xdg_dirs.append(DATADIR)
    for dir_ in xdg_dirs:
        gir_file = os.path.join(dir_, 'gir-1.0', gir_name)
        if os.path.exists(gir_file):
            return gir_file
    return None


__TRANSLATED_NAMES = {l: {} for l in OUTPUT_LANGUAGES}
__CS_INTERFACES_NAMES = {}


def get_field_c_name_components(node, components):
    parent = node.getparent()
    if parent.tag != core_ns('namespace'):
        get_field_c_name_components(parent, components)
    components.append(node.attrib.get(c_ns('type'), node.attrib['name']))


def get_field_name_components(node):
    components = []
    while node.tag != core_ns('namespace'):
        components.insert(0, node.attrib['name'])
        node = node.getparent()

    return [node.attrib['name']] + components

def get_field_c_name(node):
    components = []
    get_field_c_name_components(node, components)
    return '.'.join(components)


def __camel_case(components):
    if isinstance(components, list):
        return '.'.join([c for c in components[:-1]]) + '.' + __camel_case(components[-1])

    return ''.join(x for x in components.title() if x not in [' ', '_', '-'])

def __get_csharp_components(node, components):
    """Handle the fact that in c# a I is prepended to interface names."""
    if node.tag == core_ns('interface'):
        return components[:-1] + ['I' + components[-1]]

    elif node.getparent().tag == core_ns('interface'):
        return components[:-2] + ['I' + components[-2], components[-1]]

    return components


def set_translated_name(unique_name, **kwargs):
    override = kwargs.get('override', True)
    for lang in Lang.all():
        val = kwargs.get(lang)

        current_val = __TRANSLATED_NAMES[lang].get(unique_name)
        if current_val is None or override:
            __TRANSLATED_NAMES[lang][unique_name] = val

def make_translations(unique_name, node):
    '''Compute and store the title that should be displayed
    when linking to a given unique_name, eg in python
    when linking to test_greeter_greet() we want to display
    Test.Greeter.greet
    '''
    introspectable = not node.attrib.get('introspectable') == '0'
    c = py = js = cs = None

    if node.tag == core_ns('member'):
        c = unique_name
        if introspectable:
            components = get_gi_name_components(node)
            components[-1] = components[-1].upper()
            js = py = '.'.join(components)

            components = __get_csharp_components(node, components)
            cs = __camel_case(components)
    elif c_ns('identifier') in node.attrib:
        c =  unique_name
        if introspectable:
            components = get_gi_name_components(node)
            py = '.'.join(components)
            js = '.'.join(components[:-2] + ['prototype.%s' % components[-1]])

            components = __get_csharp_components(node, components)
            if node.tag == core_ns('constructor'):
                cs = '.'.join(components[:-1])
            elif node.tag == core_ns('function') and node.getparent().tag == core_ns('namespace'):
                cs = __camel_case([components[0], 'Global'] + components[1:])
            else:
                cs = __camel_case(components)
    elif c_ns('type') in node.attrib:
        c = unique_name
        if introspectable:
            components = get_gi_name_components(node)
            py = js = gi_name = '.'.join(components)

            components = __get_csharp_components(node, components)
            if node.tag == core_ns ('constant'):
                cs = '.'.join([components[0], 'Constants'] + components[1:])
            else:
                cs = '.'.join(components)
    elif node.tag == core_ns('field'):
        components = []
        get_field_c_name_components(node, components)
        display_name = '.'.join(components[1:])
        c = display_name
        if introspectable:
            components = get_field_name_components(node)
            py = js = '.'.join(components[1:])
            cs = __camel_case(__get_csharp_components(node, components))
    else:
        c = node.attrib.get('name')
        if introspectable:
            py = js = node.attrib.get('name')
            cs = __camel_case(node.attrib.get('name'))

    set_translated_name(unique_name, c=c, python=py, javascript=js, csharp=cs,
                        override=False)


def get_translation(unique_name, language):
    '''
    See make_translations
    '''
    return __TRANSLATED_NAMES[language].get(unique_name)


def __update_hierarchies(cur_ns, node, gi_name):
    parent_name = node.attrib.get('parent')
    if not parent_name:
        return

    if not '.' in parent_name:
        parent_name = '%s.%s' % (cur_ns, parent_name)

    __HIERARCHY_GRAPH.add_edge(parent_name, gi_name)


def __get_parent_link_recurse(gi_name, res):
    parents = __HIERARCHY_GRAPH.predecessors(gi_name)
    if parents:
        __get_parent_link_recurse(parents[0], res)
    ctype_name = ALL_GI_TYPES[gi_name]
    qs = QualifiedSymbol(type_tokens=[Link(None, ctype_name, ctype_name)])
    qs.add_extension_attribute ('gi-extension', 'type_desc',
            SymbolTypeDesc([], gi_name, ctype_name, 0, False))
    res.append(qs)


def get_klass_parents(gi_name):
    '''
    Returns a sorted list of qualified symbols representing
    the parents of the klass-like symbol named gi_name
    '''
    res = []
    parents = __HIERARCHY_GRAPH.predecessors(gi_name)
    if not parents:
        return []
    __get_parent_link_recurse(parents[0], res)
    return res


def get_klass_children(gi_name):
    '''
    Returns a dict of qualified symbols representing
    the children of the klass-like symbol named gi_name
    '''
    res = {}
    children = __HIERARCHY_GRAPH.successors(gi_name)
    for gi_name in children:
        ctype_name = ALL_GI_TYPES[gi_name]
        qs = QualifiedSymbol(type_tokens=[Link(None, ctype_name, ctype_name)])
        qs.add_extension_attribute ('gi-extension', 'type_desc',
                SymbolTypeDesc([], gi_name, ctype_name, 0, False))
        res[ctype_name] = qs
    return res

def get_unique_name_from_gapi(node):
    cname = node.attrib.get('cname')
    sep = None
    parent = node.getparent()
    if node.tag == 'virtual_method':
        sep = '::'
        parent = parent.find('class_struct')
    elif node.tag == 'property':
        sep = ':'
    elif node.tag == 'field':
        sep = '.'
    else:
        return cname

    if parent.tag == 'interface':
        parent = parent.find('class_struct')

    return parent.attrib['cname'] + sep + cname


def get_display_name_from_gapi(node, components):
    if node.tag == 'virtual_method':
        return components[-1]
    elif node.tag == 'property':
        return components[-1]
    return '.'.join(components)


def cache_gapi(node, components):
    is_ns = node.tag == 'namespace'
    is_api = node.tag == 'api'
    cname = node.attrib.get('cname')
    name = node.attrib.get('name')
    if (not cname or not name) and not is_ns and not is_api:
        return

    if not is_api:
        component_name = node.attrib['name']
        if node.tag == 'interface':
            component_name = 'I' + component_name
        components.append(component_name)
        if not is_ns:
            unique_name = get_unique_name_from_gapi(node)
            display_name = get_display_name_from_gapi(node, components)
            current_name = __TRANSLATED_NAMES[Lang.cs].get(unique_name)
            if node.attrib.get('hidden', 'false').lower() not in ['false', '0']:
                try:
                    del __TRANSLATED_NAMES[Lang.cs][unique_name]
                    print("Hide: %s => %s" % (unique_name, current_name))
                except KeyError:
                    import ipdb; ipdb.set_trace()
                    print("Already hidden: %s => %s" % (unique_name, current_name))
                    pass

            else:
                if current_name != display_name and component_name not in ['GetType', 'Constants', 'Global']:
                    print("OVERRIDE: %s => %s -> %s" % (unique_name, current_name, display_name))
                __TRANSLATED_NAMES[Lang.cs][unique_name] = display_name

    for child in node.getchildren():
        cache_gapi(child, components)
    if components:
        components.pop()

def cache_nodes(gir_root, all_girs):
    '''
    Identify and store all the gir symbols the symbols we will document
    may link to, or be typed with
    '''
    ns_node = gir_root.find('./{%s}namespace' % NS_MAP['core'])
    id_prefixes = ns_node.attrib['{%s}identifier-prefixes' % NS_MAP[Lang.c]]
    sym_prefixes = ns_node.attrib['{%s}symbol-prefixes' % NS_MAP[Lang.c]]

    id_key = '{%s}identifier' % NS_MAP[Lang.c]
    for node in gir_root.xpath(
            './/*[@c:identifier]',
            namespaces=NS_MAP):
        make_translations (node.attrib[id_key], node)

    id_type = c_ns('type')
    class_tag = core_ns('class')
    interface_tag = core_ns('interface')
    for node in gir_root.xpath(
            './/*[not(self::core:type) and not (self::core:array)][@c:type]',
            namespaces=NS_MAP):
        name = node.attrib[id_type]
        make_translations (name, node)
        gi_name = '.'.join(get_gi_name_components(node))
        ALL_GI_TYPES[gi_name] = get_klass_name(node)
        if node.tag in (class_tag, interface_tag):
            __update_hierarchies (ns_node.attrib.get('name'), node, gi_name)
            make_translations('%s::%s' % (name, name), node)
            __generate_smart_filters(id_prefixes, sym_prefixes, node)

    for field in gir_root.xpath('.//self::core:field', namespaces=NS_MAP):
        unique_name = get_field_c_name(field)
        make_translations(unique_name, field)

    for node in gir_root.xpath(
            './/core:property',
            namespaces=NS_MAP):
        name = '%s:%s' % (get_klass_name(node.getparent()),
                          node.attrib['name'])
        make_translations (name, node)

    for node in gir_root.xpath(
            './/glib:signal',
            namespaces=NS_MAP):
        name = '%s::%s' % (get_klass_name(node.getparent()),
                           node.attrib['name'])
        make_translations (name, node)

    for node in gir_root.xpath(
            './/core:virtual-method',
            namespaces=NS_MAP):
        name = get_symbol_names(node)[0]
        make_translations (name, node)

    for inc in gir_root.findall('./core:include',
            namespaces = NS_MAP):
        inc_name = inc.attrib["name"]
        inc_version = inc.attrib["version"]
        gir_file = __find_gir_file('%s-%s.gir' % (inc_name, inc_version), all_girs)
        if not gir_file:
            warn('missing-gir-include', "Couldn't find a gir for %s-%s.gir" %
                    (inc_name, inc_version))
            continue

        if gir_file in __PARSED_GIRS:
            continue

        __PARSED_GIRS.add(gir_file)
        inc_gir_root = etree.parse(gir_file).getroot()
        cache_nodes(inc_gir_root, all_girs)


def __type_tokens_from_gitype (cur_ns, ptype_name):
    qs = None

    if ptype_name == 'none':
        return None

    namespaced = '%s.%s' % (cur_ns, ptype_name)
    ptype_name = ALL_GI_TYPES.get(namespaced) or ALL_GI_TYPES.get(ptype_name) or ptype_name

    type_link = Link (None, ptype_name, ptype_name)

    tokens = [type_link]
    tokens += '*'

    return tokens


def __type_tokens_from_cdecl(cdecl):
    indirection = cdecl.count ('*')
    qualified_type = cdecl.strip ('*')
    tokens = []
    for token in qualified_type.split ():
        if token in ["const", "restrict", "volatile"]:
            tokens.append(token + ' ')
        else:
            link = Link(None, token, token)
            tokens.append (link)

    for i in range(indirection):
        tokens.append ('*')

    return tokens


def type_description_from_node(gi_node):
    '''
    Parse a typed node, returns a usable description
    '''
    ctype_name, gi_name, array_nesting = unnest_type (gi_node)

    cur_ns = get_namespace(gi_node)

    if ctype_name is not None:
        type_tokens = __type_tokens_from_cdecl (ctype_name)
    else:
        type_tokens = __type_tokens_from_gitype (cur_ns, gi_name)

    namespaced = '%s.%s' % (cur_ns, gi_name)
    if namespaced in ALL_GI_TYPES:
        gi_name = namespaced

    return SymbolTypeDesc(type_tokens, gi_name, ctype_name, array_nesting,
                          gi_node.attrib.get('direction') == 'out')


def is_introspectable(name, language):
    '''
    Do not call this before caching the nodes
    '''
    if language == Lang.c:
        return True

    if name in FUNDAMENTALS[language]:
        return True

    if __TRANSLATED_NAMES[language].get(name) is None:
        return False

    return True
