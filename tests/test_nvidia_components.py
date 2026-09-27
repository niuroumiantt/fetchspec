import unittest
from fetchspec.nvidia_components import extract, literal_object


class ComponentTests(unittest.TestCase):
    def test_extracts_only_displayed_products_without_executing_javascript(self):
        text = 'arbitrary(); const x={"RTX 5090":{DLSS:"DLSS 5","VRAM Size":"32 GB"},"RTX 3050":{DLSS:"DLSS 4"}};'
        self.assertEqual(extract(text, ['RTX 5090']), {'RTX 5090': {'DLSS': 'DLSS 5', 'VRAM Size': '32 GB'}})

    def test_rejects_expressions_and_preserves_punctuation_inside_strings(self):
        self.assertEqual(literal_object('{legacy:!0,disabled:!1}', 0), {'legacy': True, 'disabled': False})
        with self.assertRaises(ValueError): literal_object('{"value": fetch("evil")}', 0)
        self.assertEqual(literal_object('{"value": "a,{x:y}, \\"quoted\\""}', 0)['value'], 'a,{x:y}, "quoted"')
