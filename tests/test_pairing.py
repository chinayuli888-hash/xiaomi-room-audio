"""Reject unauthenticated receiver proofs before enabling encrypted streaming."""
import unittest
from unittest.mock import Mock, patch

from ap2 import tlv8
from ap2.session import AirPlay2Session


class PairingProofTests(unittest.TestCase):
    def pair(self, proof, valid):
        session = AirPlay2Session("192.0.2.10")
        session.conn.connect = Mock()
        m2 = {tlv8.TlvValue.Salt: b"salt", tlv8.TlvValue.PublicKey: b"public"}
        m4 = {} if proof is None else {tlv8.TlvValue.Proof: proof}
        session.conn.request = Mock(side_effect=[
            Mock(body=b""), Mock(body=tlv8.write_tlv(m2)), Mock(body=tlv8.write_tlv(m4)),
        ])
        srp = Mock(shared=b"k" * 64)
        srp.process.return_value = (b"a", b"m1")
        srp.verify_server_proof.return_value = valid
        return session, srp

    def test_bad_or_missing_proof_never_enables_control_keys(self):
        for proof in (b"invalid", None):
            with self.subTest(proof=proof):
                session, srp = self.pair(proof, False)
                with patch("ap2.session.TransientSrp", return_value=srp):
                    with self.assertRaisesRegex(RuntimeError, "proof verification failed"):
                        session.pair_transient()
                self.assertEqual(session.shared, b"")
                self.assertNotIn("transient pair-setup ok; control channel encrypted",
                                 session.stats.events)

    def test_verified_proof_allows_control_channel(self):
        session, srp = self.pair(b"verified", True)
        with patch("ap2.session.TransientSrp", return_value=srp):
            session.pair_transient()
        srp.verify_server_proof.assert_called_once_with(b"verified")
        self.assertEqual(len(session.shared), 64)
        self.assertIn("transient pair-setup ok; control channel encrypted", session.stats.events)
