"""Launch declarations must lead to actual, contained module execution."""
import json

import pytest

from flowmap.entries import Command, merge_entries
from flowmap.inventory import MAX_TEXT_BYTES
from flowmap.lang.ecmascript.entries import ecmascript_entries
from flowmap.lang.ecmascript.extract import extract_module


def sources(root, files):
    modules = {}
    for file, text in files.items():
        path = root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        modules[file] = extract_module(file, text.encode())
    return modules


def manifest(root, data, directory=''):
    path = root / directory / 'package.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize('launcher,file', [
    ('node', 'main.js'), ('/usr/bin/node', 'main.mjs'), ('node', 'main.cjs'),
    ('tsx', 'main.ts'), ('tsx', 'main.tsx'), ('ts-node', 'main.ts'),
    ('ts-node-esm', 'main.mts'), ('./node_modules/.bin/tsx', 'main.cts'),
])
def test_literal_launchers_use_actual_main_functions(tmp_path, launcher, file):
    modules = sources(tmp_path, {file: 'function run(){} run();'})
    found = ecmascript_entries(tmp_path, modules, [Command([launcher, file], 'docker', 'Dockerfile:2')])
    assert len(found) == 1 and found[0].target == f'{file}::<main>'
    assert found[0].kind == 'docker' and found[0].sources[0].text == 'Dockerfile:2'


def test_command_base_directory_is_authoritative(tmp_path):
    modules = sources(tmp_path, {'main.js': 'root();', 'apps/main.js': 'nested();'})
    found = ecmascript_entries(tmp_path, modules, [Command(['node', 'main.js'], 'compose', 'apps/compose.yml: api', 'apps')])
    assert [e.target for e in found] == ['apps/main.js::<main>']
    assert ecmascript_entries(tmp_path, modules, [Command(['node', 'main.js'], 'docker', 'missing/Dockerfile', 'missing')]) == []


@pytest.mark.parametrize('argv', [
    ['node', 'main.ts'], ['node', 'main.tsx'], ['node', 'missing.js'],
    ['node', 'dist/main.js'], ['echo', 'node', 'main.js'], ['sh', '-c', 'node main.js'],
    ['node', '-e', 'main.js'], ['node', '--eval=main.js'], ['node', '-p', 'main.js'],
    ['node', '$ENTRY'], ['node', '$(entry).js'], ['node', '*.js'],
    ['node', 'main.js', '&&', 'other'], ['node', 'main.js;'],
    ['node', '/outside/main.js'], ['node', '../main.js'],
    ['node', '--unknown', 'main.js'],
])
def test_unsupported_or_computed_commands_do_not_invent_entries(tmp_path, argv):
    modules = sources(tmp_path, {'main.js': 'run();', 'main.ts': 'run();', 'main.tsx': 'run();'})
    assert ecmascript_entries(tmp_path, modules, [Command(argv, 'shell', 'run.sh:1')]) == []


def test_literal_prefixes_and_known_launcher_options(tmp_path):
    modules = sources(tmp_path, {'main.js': 'run();', 'main.ts': 'run();'})
    commands = [Command(['env', 'MODE=prod', 'node', '--enable-source-maps', 'main.js'], 'docker', 'Dockerfile:2'),
                Command(['exec', 'ts-node', '--project', 'tsconfig.json', 'main.ts'], 'shell', 'run.sh:1')]
    assert {e.target for e in ecmascript_entries(tmp_path, modules, commands)} == {'main.js::<main>', 'main.ts::<main>'}


def test_nested_package_scripts_bin_and_main_resolve_from_manifest(tmp_path):
    modules = sources(tmp_path, {'apps/api/start.ts': 'start();', 'apps/api/cli.js': 'cli();', 'apps/api/index.js': 'init();'})
    manifest(tmp_path, {'name': 'api', 'scripts': {'start': 'tsx start.ts'}, 'bin': {'api': './cli.js'}, 'main': './index.js'}, 'apps/api')
    found = ecmascript_entries(tmp_path, modules, [])
    assert {(e.kind, e.target) for e in found} == {
        ('package-script', 'apps/api/start.ts::<main>'), ('package-bin', 'apps/api/cli.js::<main>'),
        ('package-main', 'apps/api/index.js::<main>')}
    assert any('start' in e.label and 'apps/api/package.json' in e.sources[0].text for e in found)


def test_string_bin_and_quoted_script_path(tmp_path):
    modules = sources(tmp_path, {'cli.js': 'cli();', 'start here.js': 'start();'})
    manifest(tmp_path, {'name': 'tool', 'bin': './cli.js', 'scripts': {'start': 'node "start here.js"'}})
    found = ecmascript_entries(tmp_path, modules, [])
    assert {e.target for e in found} == {'cli.js::<main>', 'start here.js::<main>'}
    assert any(e.label == 'tool' for e in found)


def test_missing_build_outputs_dynamic_scripts_and_library_exports_are_not_entries(tmp_path):
    modules = sources(tmp_path, {'main.ts': 'run();', 'library.js': 'export function run() {}'})
    manifest(tmp_path, {'scripts': {'start': 'node dist/main.js', 'dynamic': 'node $ENTRY', 'chain': 'node library.js && echo done', 'build': 'tsc'}, 'bin': 'missing.js', 'main': 'library.js'})
    assert ecmascript_entries(tmp_path, modules, []) == []
    assert ecmascript_entries(tmp_path, modules, [Command(['node', 'library.js'], 'docker', 'Dockerfile:1')]) == []


def test_node_package_metadata_does_not_claim_typescript_runtime(tmp_path):
    modules = sources(tmp_path, {'main.ts': 'run();'})
    manifest(tmp_path, {'main': 'main.ts', 'bin': 'main.ts'})
    assert ecmascript_entries(tmp_path, modules, []) == []


def test_deployed_and_package_declarations_merge_by_real_target(tmp_path):
    modules = sources(tmp_path, {'main.js': 'run();'})
    manifest(tmp_path, {'scripts': {'start': 'node main.js'}, 'main': 'main.js'})
    found = ecmascript_entries(tmp_path, modules, [Command(['node', 'main.js'], 'compose', 'compose.yml: api')])
    merged = merge_entries(found + found, {fid: f for m in modules.values() for fid, f in m.functions.items()})
    assert len(merged) == 1 and merged[0].kind == 'compose'
    assert {s.kind for s in merged[0].sources} == {'compose', 'package-script', 'package-main'}
    assert len(merged[0].sources) == 3


def test_only_launcher_shebangs_create_fallback_entries_outside_test_fixtures(tmp_path):
    modules = sources(tmp_path, {
        'cli.js': '#!/usr/bin/env node\nrun();', 'tool.ts': '#!/usr/bin/env tsx\nrun();',
        'ordinary.js': 'run();', 'tests/cli.js': '#!/usr/bin/env node\nrun();',
        'fixtures/cli.js': '#!/usr/bin/env node\nrun();', 'unit.test.js': '#!/usr/bin/env node\nrun();',
        'unit.spec.ts': '#!/usr/bin/env tsx\nrun();'})
    assert {e.target for e in ecmascript_entries(tmp_path, modules, [])} == {'cli.js::<main>', 'tool.ts::<main>'}


def test_manifest_and_source_symlinks_cannot_escape_project(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    project = tmp_path / 'project'
    project.mkdir()
    (outside / 'package.json').write_text('{"scripts":{"start":"node main.js"}}')
    (outside / 'main.js').write_text('run();')
    (project / 'package.json').symlink_to(outside / 'package.json')
    (project / 'main.js').symlink_to(outside / 'main.js')
    modules = {'main.js': extract_module('main.js', b'run();')}
    assert ecmascript_entries(project, modules, [Command(['node', 'main.js'], 'docker', 'Dockerfile:1')]) == []


def test_deleted_source_cannot_remain_an_entry(tmp_path):
    modules = sources(tmp_path, {'main.js': 'run();'})
    (tmp_path / 'main.js').unlink()
    manifest(tmp_path, {'main': 'main.js'})
    assert ecmascript_entries(tmp_path, modules, [Command(['node', 'main.js'], 'docker', 'Dockerfile:1')]) == []


@pytest.mark.parametrize('text', ['{', '[]', '{"scripts":42,"bin":true,"main":[]}', ' ' * (MAX_TEXT_BYTES + 1) + '{"main":"main.js"}'])
def test_malformed_and_oversize_manifests_are_ignored(tmp_path, text):
    modules = sources(tmp_path, {'main.js': 'run();'})
    (tmp_path / 'package.json').write_text(text)
    assert ecmascript_entries(tmp_path, modules, []) == []


def test_computed_shell_target_does_not_match_a_placeholder_filename(tmp_path):
    from flowmap.entries import find_commands
    modules = sources(tmp_path, {'X.js': 'run();'})
    (tmp_path / 'run.sh').write_text('node $(pick).js\n')
    assert ecmascript_entries(tmp_path, modules, find_commands(tmp_path)) == []


def test_package_entries_rank_before_shell_and_fallback_entries(tmp_path):
    modules = sources(tmp_path, {'main.js': 'run();', 'shell.js': 'work();', 'cli.js': '#!/usr/bin/env node\ncli();'})
    manifest(tmp_path, {'scripts': {'start': 'node main.js'}})
    found = ecmascript_entries(tmp_path, modules, [Command(['node', 'shell.js'], 'shell', 'run.sh:1')])
    merged = merge_entries(found, {fid: f for m in modules.values() for fid, f in m.functions.items()})
    assert [e.kind for e in merged] == ['package-script', 'shell', 'module-script']


@pytest.mark.parametrize('script', ['cd elsewhere && node main.js\n', 'cd elsewhere\nnode main.js\n'])
def test_shell_working_directory_changes_do_not_guess_source_paths(tmp_path, script):
    from flowmap.entries import find_commands
    modules = sources(tmp_path, {'main.js': 'run();'})
    (tmp_path / 'run.sh').write_text(script)
    assert ecmascript_entries(tmp_path, modules, find_commands(tmp_path)) == []


def test_literal_discovered_shell_script_still_targets_its_directory(tmp_path):
    from flowmap.entries import find_commands
    modules = sources(tmp_path, {'scripts/main.js': 'run();'})
    (tmp_path / 'scripts/run.sh').write_text('node main.js\n')
    found = ecmascript_entries(tmp_path, modules, find_commands(tmp_path))
    assert [e.target for e in found] == ['scripts/main.js::<main>']
    assert found[0].sources[0].text == 'scripts/run.sh:1'


@pytest.mark.parametrize('script', [
    'unset ENTRY\nnode "$ENTRY"\nENTRY=main.js\n',
    'ENTRY=main.js\nENTRY=$(pick)\nnode "$ENTRY"\n',
])
def test_shell_variable_expansion_does_not_fabricate_a_literal_entry(tmp_path, script):
    from flowmap.entries import find_commands
    modules = sources(tmp_path, {'main.js': 'run();'})
    (tmp_path / 'run.sh').write_text(script)
    commands = find_commands(tmp_path)
    # Shared normalization remains available to the existing Python consumer;
    # ECMAScript must reject the uncertain source of this apparently literal argv.
    assert ['node', 'main.js'] in [command.argv for command in commands]
    assert ecmascript_entries(tmp_path, modules, commands) == []


@pytest.mark.parametrize('prefix', ['builtin', 'command', 'command --'])
def test_prefixed_cwd_builtin_cannot_select_the_original_directory(tmp_path, prefix):
    from flowmap.entries import find_commands
    modules = sources(tmp_path, {'main.js': 'rootCall();', 'elsewhere/main.js': 'nestedCall();'})
    (tmp_path / 'run.sh').write_text(f'{prefix} cd elsewhere\nnode main.js\n')
    commands = find_commands(tmp_path)
    assert ['node', 'main.js'] in [command.argv for command in commands]
    assert ecmascript_entries(tmp_path, modules, commands) == []


@pytest.mark.parametrize('redirect', ["<<'END'", '<<END', '<<-END'])
def test_heredoc_payload_is_not_an_executable_launch(tmp_path, redirect):
    from flowmap.entries import find_commands
    modules = sources(tmp_path, {'main.js': 'run();'})
    (tmp_path / 'run.sh').write_text(f'cat {redirect}\nnode main.js\nEND\n')
    commands = find_commands(tmp_path)
    assert ['node', 'main.js'] in [command.argv for command in commands]
    assert ecmascript_entries(tmp_path, modules, commands) == []
