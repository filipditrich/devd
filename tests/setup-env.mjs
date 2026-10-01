import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Imported before guard.mjs so the guard reads the fixture config and a stable devd path. */
process.env.DEVD_CONFIG = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'config.json');
process.env.DEVD_BIN = '/opt/devd/bin/devd';
