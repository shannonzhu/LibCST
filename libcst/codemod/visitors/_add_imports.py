# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# pyre-strict
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

import libcst
from libcst import matchers as m, parse_statement
from libcst.codemod._context import CodemodContext
from libcst.codemod._visitor import ContextAwareTransformer
from libcst.codemod.visitors._gather_imports import GatherImportsVisitor


class AddImportsVisitor(ContextAwareTransformer):

    CONTEXT_KEY = "AddImportsVisitor"

    @staticmethod
    def _get_imports_from_context(
        context: CodemodContext,
    ) -> List[Tuple[str, Optional[str]]]:
        imports = context.scratch.get(AddImportsVisitor.CONTEXT_KEY, [])
        if not isinstance(imports, list):
            raise Exception("Logic error!")
        return imports

    @staticmethod
    def add_needed_import(
        context: CodemodContext, module: str, obj: Optional[str] = None
    ) -> None:
        if module == "__future__" and obj is None:
            raise Exception("Cannot import __future__ directly!")
        imports = AddImportsVisitor._get_imports_from_context(context)
        imports.append((module, obj))
        context.scratch[AddImportsVisitor.CONTEXT_KEY] = imports

    def __init__(
        self, context: CodemodContext, imports: Sequence[Tuple[str, Optional[str]]] = ()
    ) -> None:
        # Allow for instantiation from either a context (used when multiple transforms
        # get chained) or from a direct instantiation.
        super().__init__(context)
        imports: List[Tuple[str, Optional[str]]] = [
            *AddImportsVisitor._get_imports_from_context(context),
            *imports,
        ]

        # Verify that the imports are valid
        for module, obj in imports:
            if module == "__future__" and obj is None:
                raise Exception("Cannot import __future__ directly!")

        # List of modules we need to ensure are imported
        self.module_imports: Set[str] = {
            module for (module, obj) in imports if obj is None
        }
        # List of modules we need to check for object imports on
        from_imports: Set[str] = {
            module for (module, obj) in imports if obj is not None
        }
        # Mapping of modules we're adding to the object they should import
        self.module_mapping: Dict[str, Set[str]] = {
            module: {o for (m, o) in imports if m == module and o is not None}
            for module in sorted(from_imports)
        }
        # Track the list of imports found in the file
        self.all_imports: List[Union[libcst.Import, libcst.ImportFrom]] = []

    def visit_Module(self, node: libcst.Module) -> None:
        # Do a preliminary pass to gather the imports we already have
        gatherer = GatherImportsVisitor(self.context)
        node.visit(gatherer)
        self.all_imports = gatherer.all_imports
        self.module_imports = self.module_imports - gatherer.module_imports
        for module, imports in gatherer.object_mapping.items():
            if module not in self.module_mapping:
                # We don't care about this import at all
                continue
            elif "*" in imports:
                # We already implicitly are importing everything
                del self.module_mapping[module]
            else:
                # Lets figure out what's left to import
                self.module_mapping[module] = self.module_mapping[module] - imports
                if not self.module_mapping[module]:
                    # There's nothing left, so lets delete this work item
                    del self.module_mapping[module]

    def _get_string_name(self, node: Optional[libcst.CSTNode]) -> str:
        if node is None:
            return ""
        elif isinstance(node, libcst.Name):
            return node.value
        elif isinstance(node, libcst.Attribute):
            return self._get_string_name(node.value) + "." + node.attr.value
        else:
            raise Exception(f"Invalid node type {type(node)}!")

    def leave_ImportFrom(
        self, original_node: libcst.ImportFrom, updated_node: libcst.ImportFrom
    ) -> libcst.ImportFrom:
        if len(updated_node.relative) > 0 or updated_node.module is None:
            # Don't support relative-only imports at the moment.
            return updated_node
        if updated_node.names == "*":
            # There's nothing to do here!
            return updated_node

        # Get the module we're importing as a string, see if we have work to do
        module = self._get_string_name(updated_node.module)
        if module not in self.module_mapping:
            return updated_node

        # We have work to do, mark that we won't modify this again.
        imports_to_add = self.module_mapping[module]
        del self.module_mapping[module]

        # Now, do the actual update.
        return updated_node.with_changes(
            names=(
                *[libcst.ImportAlias(name=libcst.Name(imp)) for imp in imports_to_add],
                *updated_node.names,
            )
        )

    def _split_module(
        self, orig_module: libcst.Module, updated_module: libcst.Module
    ) -> Tuple[
        List[Union[libcst.SimpleStatementLine, libcst.BaseCompoundStatement]],
        List[Union[libcst.SimpleStatementLine, libcst.BaseCompoundStatement]],
    ]:
        import_add_location = 0

        # never insert an import before initial __strict__ flag
        if m.matches(
            orig_module,
            m.Module(
                body=[
                    m.SimpleStatementLine(
                        body=[
                            m.Assign(
                                targets=[m.AssignTarget(target=m.Name("__strict__"))]
                            )
                        ]
                    ),
                    m.ZeroOrMore(),
                ]
            ),
        ):
            import_add_location = 1

        # This works under the principle that while we might modify node contents,
        # we have yet to modify the number of statements. So we can match on the
        # original tree but break up the statements of the modified tree. If we
        # change this assumption in this visitor, we will have to change this code.
        for i, statement in enumerate(orig_module.body):
            if isinstance(statement, libcst.SimpleStatementLine):
                for possible_import in statement.body:
                    for last_import in self.all_imports:
                        if possible_import is last_import:
                            import_add_location = i + 1
                            break

        return (
            list(updated_module.body[:import_add_location]),
            list(updated_module.body[import_add_location:]),
        )

    def _insert_empty_line(
        self,
        statements: List[
            Union[libcst.SimpleStatementLine, libcst.BaseCompoundStatement]
        ],
    ) -> List[Union[libcst.SimpleStatementLine, libcst.BaseCompoundStatement]]:
        if len(statements) < 1:
            # No statements, nothing to add to
            return statements
        if len(statements[0].leading_lines) == 0:
            # Statement has no leading lines, add one!
            return [
                statements[0].with_changes(leading_lines=(libcst.EmptyLine(),)),
                *statements[1:],
            ]
        if statements[0].leading_lines[0].comment is None:
            # First line is empty, so its safe to leave as-is
            return statements
        # Statement has a comment first line, so lets add one more empty line
        return [
            statements[0].with_changes(
                leading_lines=(libcst.EmptyLine(), *statements[0].leading_lines)
            ),
            *statements[1:],
        ]

    def leave_Module(
        self, original_node: libcst.Module, updated_node: libcst.Module
    ) -> libcst.Module:
        # Don't try to modify if we have nothing to do
        if not self.module_imports and not self.module_mapping:
            return updated_node

        # First, find the insertion point for imports
        statements_before_imports, statements_after_imports = self._split_module(
            original_node, updated_node
        )

        # Make sure there's at least one empty line before the first non-import
        statements_after_imports = self._insert_empty_line(statements_after_imports)

        # Now, add all of the imports we need!
        return updated_node.with_changes(
            body=(
                *[
                    parse_statement(
                        f"from {module} import {', '.join(sorted(imports))}",
                        config=updated_node.config_for_parsing,
                    )
                    for module, imports in self.module_mapping.items()
                    if module == "__future__"
                ],
                *statements_before_imports,
                *[
                    parse_statement(
                        f"import {module}", config=updated_node.config_for_parsing
                    )
                    for module in sorted(self.module_imports)
                ],
                *[
                    parse_statement(
                        f"from {module} import {', '.join(sorted(imports))}",
                        config=updated_node.config_for_parsing,
                    )
                    for module, imports in self.module_mapping.items()
                    if module != "__future__"
                ],
                *statements_after_imports,
            )
        )
