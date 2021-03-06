# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# pyre-strict
import ast
from typing import Generator, List, Optional, Sequence, Set, Tuple

import libcst as cst
import libcst.matchers as m
from libcst.codemod import (
    CodemodContext,
    ContextAwareTransformer,
    ContextAwareVisitor,
    VisitorBasedCodemodCommand,
)


def _get_lhs(field: cst.BaseExpression) -> cst.BaseExpression:
    if isinstance(field, (cst.Name, cst.Integer)):
        return field
    elif isinstance(field, (cst.Attribute, cst.Subscript)):
        return _get_lhs(field.value)
    else:
        raise Exception("Unsupported node type!")


def _find_expr_from_field_name(
    fieldname: str, args: Sequence[cst.Arg]
) -> Optional[cst.BaseExpression]:
    # Things like "0.name" are invalid expressions in python since
    # we can't tell if name is supposed to be the fraction or a name.
    # So we do a trick to parse here where we wrap the LHS in parens
    # and assume LibCST will handle it.
    if "." in fieldname:
        ind, exp = fieldname.split(".", 1)
        fieldname = f"({ind}).{exp}"
    field_expr = cst.parse_expression(fieldname)
    lhs = _get_lhs(field_expr)

    # Verify we don't have any *args or **kwargs attributes.
    if any(arg.star != "" for arg in args):
        return None

    # Get the index into the arg
    index: Optional[int] = None
    if isinstance(lhs, cst.Integer):
        index = int(lhs.value)
        if index < 0 or index >= len(args):
            raise Exception(f"Logic error, arg sequence {index} out of bounds!")
    elif isinstance(lhs, cst.Name):
        for i, arg in enumerate(args):
            kw = arg.keyword
            if kw is None:
                continue
            if kw.value == lhs.value:
                index = i
                break
        if index is None:
            raise Exception(f"Logic error, arg name {lhs.value} out of bounds!")

    if index is None:
        raise Exception(f"Logic error, unsupported fieldname expression {fieldname}!")

    # Format it!
    return field_expr.deep_replace(lhs, args[index].value)


def _string_prefix_and_quotes(string: str) -> Tuple[str, str, str]:
    prefix: str = ""
    quote: str = ""
    pos: int = 0

    for i in range(0, len(string)):
        if string[i] in {"'", '"'}:
            pos = i
            break
        prefix += string[i]

    for i in range(pos, len(string)):
        if string[i] not in {"'", '"'}:
            break
        if quote and string[i] != quote[0]:
            # This is no longer the same string quote
            break
        quote += string[i]

    if len(quote) == 2:
        # Lets assume this is an empty string.
        quote = quote[:1]
    elif len(quote) == 6:
        # Lets assume this is an empty string.
        quote = quote[:3]
    if len(quote) not in {1, 3}:
        raise Exception(f"Invalid string {string}")

    innards = string[(len(prefix) + len(quote)) : (-len(quote))]

    return prefix, quote, innards


def _get_field(formatstr: str) -> Tuple[str, Optional[str], Optional[str]]:
    in_index: int = 0
    format_spec: Optional[str] = None
    conversion: Optional[str] = None

    # Grab any format spec as long as its not an array slice
    for pos, char in enumerate(formatstr):
        if char == "[":
            in_index += 1
        elif char == "]":
            in_index -= 1
        elif char == ":":
            if in_index == 0:
                formatstr, format_spec = (formatstr[:pos], formatstr[pos + 1 :])
                break

    # Grab any conversion
    if "!" in formatstr:
        formatstr, conversion = formatstr.split("!", 1)

    # Return it
    return formatstr, format_spec, conversion


def _get_tokens(  # noqa: C901
    string: str,
) -> Generator[Tuple[str, Optional[str], Optional[str], Optional[str]], None, None]:
    length = len(string)
    prefix: str = ""
    format_accum: str = ""
    in_brackets: int = 0
    seen_escape: bool = False

    for pos, char in enumerate(string):
        if seen_escape:
            # The last character was an escape character, so consume
            # this one as well, and then pop out of the escape.
            if in_brackets == 0:
                prefix += char
            else:
                format_accum += char
            seen_escape = False
            continue

        # We can't escape inside a f-string/format specifier.
        if in_brackets == 0:
            # Grab the next character to see if we are an escape sequence.
            next_char: Optional[str] = None
            if pos < length - 1:
                next_char = string[pos + 1]

            # If this current character is an escape, we want to
            # not react to it, append it to the current accumulator and
            # then do the same for the next character.
            if char == "{" and next_char == "{":
                seen_escape = True
            if char == "}" and next_char == "}":
                seen_escape = True

        # Only if we are not an escape sequence do we consider these
        # brackets.
        if not seen_escape:
            if char == "{":
                in_brackets += 1

                # We want to add brackets to the format accumulator as
                # long as they aren't the outermost, because format
                # specs allow {} expansion.
                if in_brackets == 1:
                    continue
            if char == "}":
                in_brackets -= 1

                if in_brackets < 0:
                    raise Exception("Stray } in format string!")

                if in_brackets == 0:
                    field_name, format_spec, conversion = _get_field(format_accum)
                    yield (prefix, field_name, format_spec, conversion)

                    prefix = ""
                    format_accum = ""
                    continue

        # Place in the correct accumulator
        if in_brackets == 0:
            prefix += char
        else:
            format_accum += char

    if in_brackets > 0:
        raise Exception("Stray { in format string!")
    if format_accum:
        raise Exception("Logic error!")

    # Yield the last bit of information
    yield (prefix, None, None, None)


class StringQuoteGatherer(ContextAwareVisitor):
    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)
        self.stringends: Set[str] = set()

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        self.stringends.add(node.value[-1])


class StripNewlinesTransformer(ContextAwareTransformer):
    def leave_ParenthesizedWhitespace(
        self,
        original_node: cst.ParenthesizedWhitespace,
        updated_node: cst.ParenthesizedWhitespace,
    ) -> cst.SimpleWhitespace:
        return cst.SimpleWhitespace(" ")


class SwitchStringQuotesTransformer(ContextAwareTransformer):
    def __init__(self, context: CodemodContext, avoid_quote: str) -> None:
        super().__init__(context)
        if avoid_quote not in {'"', "'"}:
            raise Exception("Must specify either ' or \" single quote to avoid.")
        self.avoid_quote: str = avoid_quote
        self.replace_quote: str = '"' if avoid_quote == "'" else "'"

    def leave_SimpleString(
        self, original_node: cst.SimpleString, updated_node: cst.SimpleString
    ) -> cst.SimpleString:
        prefix, quote, innards = _string_prefix_and_quotes(updated_node.value)
        if self.avoid_quote in quote:
            # Attempt to swap the value out, verify that the string is still identical
            # before and after transformation.
            new_quote = quote.replace(self.avoid_quote, self.replace_quote)
            new_value = f"{prefix}{new_quote}{innards}{new_quote}"

            try:
                old_str = ast.literal_eval(updated_node.value)
                new_str = ast.literal_eval(new_value)

                if old_str != new_str:
                    # This isn't the same!
                    return updated_node

                return updated_node.with_changes(value=new_value)
            except Exception:
                # Failed to parse string, changing the quoting screwed us up.
                pass

        # Either failed to parse the new string, or don't need to make changes.
        return updated_node


class ConvertFormatStringCommand(VisitorBasedCodemodCommand):

    DESCRIPTION: str = "Converts instances of str.format() to f-string."

    def leave_Call(  # noqa: C901
        self, original_node: cst.Call, updated_node: cst.Call
    ) -> cst.BaseExpression:
        # Lets figure out if this is a "".format() call
        if self.matches(
            updated_node,
            m.Call(func=m.Attribute(value=m.SimpleString(), attr=m.Name("format"))),
        ):
            fstring: List[cst.BaseFormattedStringContent] = []
            inserted_sequence: int = 0

            # TODO: Use `extract` when it becomes available.
            stringvalue = cst.ensure_type(
                cst.ensure_type(updated_node.func, cst.Attribute).value,
                cst.SimpleString,
            ).value
            prefix, quote, innards = _string_prefix_and_quotes(stringvalue)
            tokens = _get_tokens(innards)
            for (literal_text, field_name, format_spec, conversion) in tokens:
                if literal_text:
                    fstring.append(cst.FormattedStringText(literal_text))
                if field_name is None:
                    # This is not a format-specification
                    continue
                if format_spec is not None and len(format_spec) > 0:
                    # TODO: This is supportable since format specs are compatible
                    # with f-string format specs, but it would require matching
                    # format specifier expansions.
                    self.warn(f"Unsupported format_spec {format_spec} in format() call")
                    return updated_node

                # Auto-insert field sequence if it is empty
                if field_name == "":
                    field_name = str(inserted_sequence)
                    inserted_sequence += 1
                expr = _find_expr_from_field_name(field_name, updated_node.args)
                if expr is None:
                    # Most likely they used * expansion in a format.
                    self.warn(f"Unsupported field_name {field_name} in format() call")
                    return updated_node

                # Verify that we don't have any comments or newlines. Comments aren't
                # allowed in f-strings, and newlines need parenthesization. We can
                # have formattedstrings inside other formattedstrings, but I chose not
                # to doeal with that for now.
                if self.findall(expr, m.Comment()):
                    # We could strip comments, but this is a formatting change so
                    # we choose not to for now.
                    self.warn(f"Unsupported comment in format() call")
                    return updated_node
                if self.findall(expr, m.FormattedString()):
                    self.warn(f"Unsupported f-string in format() call")
                    return updated_node
                if self.findall(expr, m.Await()):
                    # This is fixed in 3.7 but we don't currently have a flag
                    # to enable/disable it.
                    self.warn(f"Unsupported await in format() call")
                    return updated_node

                # Stripping newlines is effectively a format-only change.
                expr = cst.ensure_type(
                    expr.visit(StripNewlinesTransformer(self.context)),
                    cst.BaseExpression,
                )

                # Try our best to swap quotes on any strings that won't fit
                expr = cst.ensure_type(
                    expr.visit(SwitchStringQuotesTransformer(self.context, quote[0])),
                    cst.BaseExpression,
                )

                # Verify that the resulting expression doesn't have a backslash
                # in it.
                raw_expr_string = self.module.code_for_node(expr)
                if "\\" in raw_expr_string:
                    self.warn(f"Unsupported backslash in format expression")
                    return updated_node

                # For safety sake, if this is a dict/set or dict/set comprehension,
                # wrap it in parens so that it doesn't accidentally create an
                # escape.
                if (
                    raw_expr_string.startswith("{") or raw_expr_string.endswith("}")
                ) and (not expr.lpar or not expr.rpar):
                    expr = expr.with_changes(
                        lpar=[cst.LeftParen()], rpar=[cst.RightParen()]
                    )

                # Verify that any strings we insert don't have the same quote
                quote_gatherer = StringQuoteGatherer(self.context)
                expr.visit(quote_gatherer)
                for stringend in quote_gatherer.stringends:
                    if stringend in quote:
                        self.warn(
                            f"Cannot embed string with same quote from format() call"
                        )
                        return updated_node

                fstring.append(
                    cst.FormattedStringExpression(
                        expression=expr, conversion=conversion
                    )
                )
            if quote not in ['"', '"""', "'", "'''"]:
                raise Exception(f"Invalid f-string quote {quote}")
            return cst.FormattedString(
                parts=fstring,
                start=f"f{prefix}{quote}",
                # pyre-ignore I know what I'm doing with end, so no Literal[str]
                # here. We get the string start/end from the original SimpleString
                # so we know its correct.
                end=quote,
            )

        return updated_node
