# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# pyre-strict
from libcst.codemod import CodemodTest
from libcst.codemod.commands.ensure_import_present import EnsureImportPresentCommand


class EnsureImportPresentCommandTest(CodemodTest):
    TRANSFORM = EnsureImportPresentCommand

    def test_import_module(self) -> None:
        before = ""
        after = "import a"
        self.assertCodemod(before, after, module="a", entity=None)

    def test_import_entity(self) -> None:
        before = ""
        after = "from a import b"
        self.assertCodemod(before, after, module="a", entity="b")

    def test_import_wildcard(self) -> None:
        before = "from a import *"
        after = "from a import *"
        self.assertCodemod(before, after, module="a", entity="b")
