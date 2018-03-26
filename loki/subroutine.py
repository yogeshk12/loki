from collections import Mapping

from loki.generator import generate, extract_source
from loki.ir import (Declaration, Allocation, Import, Statement, TypeDef,
                     Conditional, CommentBlock)
from loki.expression import ExpressionVisitor
from loki.types import DerivedType
from loki.visitors import FindNodes
from loki.tools import flatten


__all__ = ['Section', 'Subroutine', 'Module']


class InsertLiteralKinds(ExpressionVisitor):
    """
    Re-insert explicit _KIND casts for literals dropped during pre-processing.

    :param pp_info: List of `(literal, kind)` tuples to be inserted
    """

    def __init__(self, pp_info):
        super(InsertLiteralKinds, self).__init__()

        self.pp_info = dict(pp_info)

    def visit_Literal(self, o):
        if o._source.lines[0] in self.pp_info:
            literals = dict(self.pp_info[o._source.lines[0]])
            if o.value in literals:
                o.value = '%s_%s' % (o.value, literals[o.value])

    def visit_CommentBlock(self, o):
        for c in o.comments:
            self.visit(c)

    def visit_Comment(self, o):
        if o._source.lines[0] in self.pp_info:
            for val, kind in self.pp_info[o._source.lines[0]]:
                o._source.string = o._source.string.replace(val, '%s_%s' % (val, kind))


class Section(object):
    """
    Class to handle and manipulate a source code section.

    :param name: Name of the source section.
    :param source: String with the contained source code.
    """

    def __init__(self, name, source):
        self.name = name

        self._source = source

    @property
    def source(self):
        """
        The raw source code contained in this section.
        """
        return self._source

    @property
    def lines(self):
        """
        Sanitizes source content into long lines with continuous statements.

        Note: This does not change the content of the file
        """
        return self._source.splitlines(keepends=True)

    def replace(self, repl, new=None):
        """
        Performs a line-by-line string-replacement from a given mapping

        Note: The replacement is performed on each raw line. Might
        need to improve this later to unpick linebreaks in the search
        keys.
        """
        if isinstance(repl, Mapping):
            for old, new in repl.items():
                self._source = self._source.replace(old, new)
        else:
            self._source = self._source.replace(repl, new)


class Module(Section):
    """
    Class to handle and manipulate source modules.

    :param name: Name of the module
    :param ast: OFP parser node for this module
    :param raw_source: Raw source string, broken into lines(!), as it
                       appeared in the parsed source file.
    """

    def __init__(self, name=None, spec=None, routines=None, ast=None,
                 raw_source=None):
        self.name = name or ast.attrib['name']
        self.spec = spec
        self.routines = routines

        self._ast = ast
        self._raw_source = raw_source

    @classmethod
    def from_source(cls, ast, raw_source, name=None):
        # Process module-level type specifications
        name = name or ast.attrib['name']

        # Parse type definitions into IR and store
        spec_ast = ast.find('body/specification')
        spec = generate(spec_ast, raw_source)

        # TODO: Add routine parsing

        # Process pragmas to override deferred dimensions
        cls._process_pragmas(spec)

        return cls(name=name, spec=spec, ast=ast, raw_source=raw_source)

    @classmethod
    def _process_pragmas(self, spec):
        """
        Process any '!$ecir dimension' pragmas to override deferred dimensions
        """
        for typedef in FindNodes(TypeDef).visit(spec):
            pragmas = {p._source.lines[0]: p for p in typedef.pragmas}
            for v in typedef.variables:
                if v._source.lines[0]-1 in pragmas:
                    pragma = pragmas[v._source.lines[0]-1]
                    if pragma.keyword == 'dimension':
                        # Found dimension override for variable
                        dims = pragma._source.string.split('dimension(')[-1]
                        dims = dims.split(')')[0].split(',')
                        dims = [d.strip() for d in dims]
                        # Override dimensions (hacky: not transformer-safe!)
                        v.dimensions = dims

    @property
    def typedefs(self):
        """
        Map of names and :class:`DerivedType`s defined in this module.
        """
        types = FindNodes(TypeDef).visit(self.spec)
        return {td.name.upper(): td for td in types}


class Subroutine(Section):
    """
    Class to handle and manipulate a single subroutine.

    :param name: Name of the subroutine
    :param ast: OFP parser node for this subroutine
    :param raw_source: Raw source string, broken into lines(!), as it
                       appeared in the parsed source file.
    :param typedefs: Optional list of external definitions for derived
                     types that allows more detaild type information.
    """

    def __init__(self, ast, raw_source, name=None, typedefs=None, pp_info=None):
        self.name = name or ast.attrib['name']
        self._ast = ast
        self._raw_source = raw_source

        # The actual lines in the source for this subroutine
        # TODO: Turn Section._source into a real `Source` object
        self._source = extract_source(self._ast.attrib, raw_source).string

        # Separate body and declaration sections
        # Note: The declaration includes the SUBROUTINE key and dummy
        # variable list, so no _pre section is required.
        body_ast = self._ast.find('body')
        bend = int(body_ast.attrib['line_end'])
        spec_ast = self._ast.find('body/specification')
        sstart = int(spec_ast.attrib['line_begin']) - 1
        send = int(spec_ast.attrib['line_end'])
        self.header = Section(name='header', source=''.join(self.lines[:sstart]))
        self.declarations = Section(name='declarations', source=''.join(self.lines[sstart:send]))
        self.body = Section(name='body', source=''.join(self.lines[send:bend]))
        self._post = Section(name='post', source=''.join(self.lines[bend:]))

        # Create a IRs for declarations section and the loop body
        self._ir = generate(self._ast.find('body'), self._raw_source)

        # Store the names of variables in the subroutine signature
        arg_ast = self._ast.findall('header/arguments/argument')
        self._argnames = [arg.attrib['name'] for arg in arg_ast]

        # Attach derived-type information to variables from given typedefs
        for v in self.variables:
            if typedefs is not None and v.type.name in typedefs:
                typedef = typedefs[v.type.name]
                derived_type = DerivedType(name=typedef.name, variables=typedef.variables,
                                           intent=v.type.intent, allocatable=v.type.allocatable,
                                           pointer=v.type.pointer, optional=v.type.optional)
                v._type = derived_type

        # Re-insert literal _KIND type casts from pre-processing info
        # Note, that this is needed to get accurate data _KIND
        # attributes for literal values, as these have been stripped
        # in a preprocessing step to avoid OFP bugs.
        if pp_info is not None:
            insert_kind = InsertLiteralKinds(pp_info)

            for decl in FindNodes(Declaration).visit(self.ir):
                for v in decl.variables:
                    if v.initial is not None:
                        insert_kind.visit(v.initial)

            for stmt in FindNodes(Statement).visit(self.ir):
                insert_kind.visit(stmt)

            for cnd in FindNodes(Conditional).visit(self.ir):
                for c in cnd.conditions:
                    insert_kind.visit(c)

            for cmt in FindNodes(CommentBlock).visit(self.ir):
                insert_kind.visit(cmt)

        # And finally we parse "member" subroutines
        self.members = None
        if self._ast.find('members'):
            self.members = [Subroutine(ast=s, raw_source=self._raw_source,
                                       typedefs=typedefs, pp_info=pp_info)
                            for s in self._ast.findall('members/subroutine')]

    def _infer_variable_dimensions(self):
        """
        Try to infer variable dimensions for ALLOCATABLEs
        """
        allocs = FindNodes(Allocation).visit(self.ir)
        for v in self.variables:
            if v.type.allocatable:
                alloc = [a for a in allocs if a.variable.name == v.name]
                if len(alloc) > 0:
                    v.dimensions = alloc[0].variable.dimensions

    @property
    def source(self):
        """
        The raw source code contained in this section.
        """
        content = [self.header, self.declarations, self.body, self._post]
        return ''.join(s.source for s in content)

    @property
    def ir(self):
        """
        Intermediate representation (AST) of the body in this subroutine
        """
        return self._ir

    @property
    def argnames(self):
        return self._argnames

    @property
    def arguments(self):
        """
        List of argument names as defined in the subroutine signature.
        """
        vmap = self.variable_map
        return [vmap[name] for name in self.argnames]

    @property
    def variables(self):
        """
        List of all declared variables
        """
        decls = FindNodes(Declaration).visit(self.ir)
        return flatten([d.variables for d in decls])

    @property
    def variable_map(self):
        """
        Map of variable names to `Variable` objects
        """
        return {v.name: v for v in self.variables}

    @property
    def imports(self):
        """
        List of all module imports via USE statements
        """
        return FindNodes(Import).visit(self.ir)