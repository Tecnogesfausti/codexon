import json,unittest
from pathlib import Path
import yaml

class ContainerStartupTests(unittest.TestCase):
 def test_host_pid_configuration_bypasses_inherited_s6_init(self):
  addon=Path(__file__).resolve().parents[2];config=yaml.safe_load((addon/'config.yaml').read_text());docker=(addon/'Dockerfile').read_text();self.assertTrue(config['host_pid']);self.assertFalse(config['init'])
  entrypoints=[json.loads(line.split(' ',1)[1]) for line in docker.splitlines() if line.startswith('ENTRYPOINT ')];commands=[json.loads(line.split(' ',1)[1]) for line in docker.splitlines() if line.startswith('CMD ')]
  self.assertEqual(entrypoints[-1],['/usr/local/bin/codexon-entrypoint']);self.assertEqual(commands[-1],[])
  script=addon/'rootfs/usr/local/bin/codexon-entrypoint';self.assertIn("/usr/local/bin/codexon-entrypoint",docker.split("RUN chmod +x",1)[1].split("\n\n",1)[0]);self.assertIn('exec nginx -g "daemon off;"',script.read_text())
 def test_release_version_matches_image_and_runtime_migration(self):
  addon=Path(__file__).resolve().parents[2];version=yaml.safe_load((addon/'config.yaml').read_text())['version'];self.assertIn('ARG BUILD_VERSION='+version,(addon/'Dockerfile').read_text());self.assertIn('runtime-'+version,(addon/'rootfs/usr/local/bin/codexon-entrypoint').read_text())
