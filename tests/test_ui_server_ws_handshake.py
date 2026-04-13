import unittest

from focusfield.ui.server import _ws_accept_key


class UIServerWebSocketHandshakeTests(unittest.TestCase):
    def test_accept_key_matches_rfc_example(self) -> None:
        self.assertEqual(
            _ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_accept_key_ignores_header_whitespace(self) -> None:
        self.assertEqual(
            _ws_accept_key("  dGhlIHNhbXBsZSBub25jZQ==  "),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


if __name__ == "__main__":
    unittest.main()
