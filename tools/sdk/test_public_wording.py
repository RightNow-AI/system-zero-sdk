"""Release controls for public declarations and wording."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('wording_rules', Path(__file__).with_name('public_content_rules.py'))
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

TERM_CODES = [[114, 101, 100, 105, 115], [112, 111, 115, 116, 103, 114, 101, 115], [112, 111, 115, 116, 103, 114, 101, 115, 113, 108], [108, 117, 97], [97, 119, 115], [114, 50], [111, 109, 110, 105], [106, 101, 118], [115, 117, 112, 97, 98, 97, 115, 101], [117, 112, 115, 116, 97, 115, 104], [118, 101, 114, 99, 101, 108], [102, 108, 121, 46, 105, 111], [102, 108, 121, 105, 111], [102, 108, 121, 46, 100, 101, 118], [109, 111, 100, 97, 108], [109, 111, 100, 97, 108, 46, 114, 117, 110], [110, 101, 111, 110], [99, 108, 111, 117, 100, 102, 108, 97, 114, 101], [98, 101, 116, 116, 101, 114, 115, 116, 97, 99, 107], [98, 101, 116, 116, 101, 114, 32, 115, 116, 97, 99, 107], [112, 111, 115, 116, 104, 111, 103], [114, 101, 115, 101, 110, 100], [115, 116, 114, 105, 112, 101], [115, 101, 110, 116, 114, 121], [100, 97, 116, 97, 100, 111, 103], [114, 97, 105, 108, 119, 97, 121], [114, 101, 110, 100, 101, 114, 46, 99, 111, 109], [113, 119, 101, 110], [100, 101, 101, 112, 115, 101, 101, 107], [108, 108, 97, 109, 97], [109, 105, 115, 116, 114, 97, 108], [103, 101, 109, 105, 110, 105], [103, 112, 116], [99, 108, 97, 117, 100, 101], [97, 110, 116, 104, 114, 111, 112, 105, 99], [111, 112, 101, 110, 97, 105], [118, 108, 108, 109], [115, 103, 108, 97, 110, 103], [98, 50, 48, 48], [98, 51, 48, 48], [104, 49, 48, 48], [104, 50, 48, 48], [97, 49, 48, 48], [110, 118, 105, 100, 105, 97], [99, 117, 100, 97], [103, 112, 117], [100, 101, 99, 105, 100, 101, 114], [114, 117, 110, 105, 110, 102, 114, 97], [114, 117, 110, 112, 105, 112, 101], [111, 114, 110, 105, 116, 104], [111, 114, 110, 110], [108, 111, 110, 103, 109], [115, 122, 50], [115, 122, 112], [115, 121, 115, 116, 101, 109, 50], [115, 121, 115, 116, 101, 109, 32, 111, 110, 101], [102, 111, 114, 103, 101], [114, 105, 103, 104, 116, 110, 111, 119, 45, 97, 112, 105], [105, 110, 102, 114, 97, 45, 115, 116, 114, 101, 97, 109, 105, 110, 103], [99, 117, 115, 116, 111, 100, 121], [115, 101, 97, 108, 101, 100], [104, 111, 108, 100, 111, 117, 116], [49, 112, 97, 115, 115, 119, 111, 114, 100], [111, 112, 58, 47, 47], [98, 101, 116, 97], [112, 114, 101, 118, 105, 101, 119], [99, 111, 109, 105, 110, 103, 32, 115, 111, 111, 110], [101, 97, 114, 108, 121, 32, 97, 99, 99, 101, 115, 115], [101, 120, 112, 101, 114, 105, 109, 101, 110, 116, 97, 108], [97, 108, 112, 104, 97], [112, 105, 108, 111, 116], [119, 97, 105, 116, 108, 105, 115, 116], [110, 111, 116, 32, 121, 101, 116, 32, 97, 118, 97, 105, 108, 97, 98, 108, 101], [110, 111, 116, 32, 97, 118, 97, 105, 108, 97, 98, 108, 101, 32, 121, 101, 116]]

class PublicWordingTests(unittest.TestCase):
    def test_planted_terms_fail_in_names_and_prose(self):
        for codes in TERM_CODES:
            term = ''.join(map(chr, codes))
            for text in ('export const ' + term.upper().replace(' ', '_') + '_TOOLS = [];',
                         '/** This SDK uses ' + term + '. */', 'description = "' + term + ' release"'):
                self.assertIn('public-wording', rules.public_rule_ids(text, 'typescript/src/example.ts'))

    def test_only_exact_wire_field_is_exempt(self):
        marker = ''.join(map(chr, (112,114,101,118,105,101,119)))
        self.assertEqual(rules.public_wording_rule_ids('export type Answer = { "MARKER"?: true; };'.replace('MARKER', marker), 'typescript/src/api-types.ts'), [])
        self.assertIn('public-wording', rules.public_wording_rule_ids('/** MARKER client */'.replace('MARKER', marker), 'typescript/src/api-types.ts'))
        for declaration in ('export const MARKER = true;', 'export const MARKER=true;'):
            self.assertIn('public-wording', rules.public_wording_rule_ids(declaration.replace('MARKER', marker), 'typescript/src/api-types.ts'))

    def test_neutral_prose_passes(self):
        self.assertEqual(rules.public_wording_rule_ids('Function definitions. Production/Stable. Evaluate input.', 'README.md'), [])

if __name__ == '__main__':
    unittest.main()
