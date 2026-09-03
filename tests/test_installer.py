"""Exercise the exact embedded installer port editor without touching Docker."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AWK = shutil.which("awk")
if not AWK and Path("D:/Git/usr/bin/awk.exe").exists():
    AWK = "D:/Git/usr/bin/awk.exe"


@unittest.skipUnless(AWK, "awk is required")
class InstallerPortTests(unittest.TestCase):
    def edit(self, source, port="18002"):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        program = script.split('awk -v port="${WEB_PORT}" \'\n', 1)[1].split(
            '\n\' "${COMPOSE_FILE}"', 1
        )[0]
        with tempfile.TemporaryDirectory(prefix="AWBotNest V2 ") as folder:
            config = Path(folder) / "docker-compose.yml"
            config.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [AWK, "-v", f"port={port}", program, str(config)],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(config.read_text(encoding="utf-8"), source)
            return result

    def test_only_web_mapping_changes(self):
        source = '''services:
  awbotnest:
    image: awdress/awbotnest_v2:latest
    environment:
      - EXAMPLE=18001:18001
    ports:
      - "18001:18001" # web
      - "9090:9090"
    volumes:
      - ./data:/app/data
  other:
    ports:
      - "18001:18001"
'''
        result = self.edit(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, source.replace('"18001:18001" # web', '"18002:18001" # web'))

    def test_preserves_bind_address_and_tcp(self):
        for mapping in ['"127.0.0.1:18001:18001/tcp"', "'[::1]:18001:18001'", '18001:18001']:
            with self.subTest(mapping=mapping):
                source = f"services:\n  awbotnest:\n    ports:\n      - {mapping}\n"
                result = self.edit(source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, source.replace("18001:18001", "18002:18001"))

    def test_rejects_ambiguous_or_unsupported_mapping(self):
        for ports in [
            '      - "18001:18001"\n      - "18002:18001"\n',
            '      - target: 18001\n        published: "18001"\n',
            '      - "${PORT}:18001"\n',
            '      - "18001:18001/udp"\n',
            '      - "9000:9000" # 18001:18001\n',
        ]:
            with self.subTest(ports=ports):
                result = self.edit("services:\n  awbotnest:\n    ports:\n" + ports)
                self.assertNotEqual(result.returncode, 0)

    def test_idempotent_and_no_other_service_selection(self):
        source = 'services:\n  awbotnest:\n    ports:\n      - "18002:18001"\n'
        self.assertEqual(self.edit(source).stdout, source)
        self.assertNotEqual(self.edit(source.replace("awbotnest:", "other:")).returncode, 0)


if __name__ == "__main__":
    unittest.main()
