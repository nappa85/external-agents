// @amp-agent-mode {"key": "__MODE__", "label": "__MODE__"}
// The amp reviewer's mode, generated into the snapshot by review.py, which fills in the
// placeholders between double underscores. Plain JavaScript, so tests can run it under node.
//
// The agent gets Read, create_file and edit_file, plus two search tools defined here,
// since Amp has no grep or glob tool. apply_patch is left out because its paths are inside
// the patch text. In the bubblewrap sandbox, which confines commands itself, it also gets
// the shell tools. The tool.call hook decides every call: a tool must be one of these, a
// file tool's path absolute and, links resolved, inside the snapshot, and writes to `.amp/`
// are refused so the reviewer cannot rewrite this file. A plugin that fails to load leaves
// no reviewer mode, so the run fails instead of going unchecked.
import { lstatSync, readdirSync, readFileSync, realpathSync } from 'node:fs'
import path from 'node:path'

const ROOT = realpathSync.native(__ROOT__)
const CONFIG = path.join(ROOT, '.amp')
const SHELL = __SHELL__
const SEARCH_TOOLS = new Set(['search_files', 'find_files'])
const SHELL_TOOLS = new Set(SHELL ? ['shell_command', 'shell_command_status', 'shell_command_kill'] : [])
const TOOLS = new Set(['Read', 'create_file', 'edit_file', ...SEARCH_TOOLS, ...SHELL_TOOLS])
// Results per search, and the largest file searched.
const LIMIT = 200
const MAX_FILE = 1 << 20

function within(dir, target) {
	return target === dir || target.startsWith(dir + path.sep)
}

// Where `target` really is: the real path of its deepest existing ancestor plus the
// components that do not exist yet. Any other failure, such as a dangling link that a
// write would follow, gives null.
function realLocation(target) {
	const missing = []
	for (let head = target; ; ) {
		try {
			return path.join(realpathSync.native(head), ...missing)
		} catch (error) {
			if (error.code !== 'ENOENT') return null
		}
		try {
			lstatSync(head)
			return null
		} catch {}
		const parent = path.dirname(head)
		if (parent === head) return null
		missing.unshift(path.basename(head))
		head = parent
	}
}

// Why the call must not run, or null. A search without a path covers the whole snapshot.
function refusal(tool, input) {
	if (!TOOLS.has(tool)) return `Only ${[...TOOLS].join(', ')} are available.`
	if (SHELL_TOOLS.has(tool)) return null
	const target = input.path ?? (SEARCH_TOOLS.has(tool) ? ROOT : undefined)
	if (typeof target !== 'string' || !path.isAbsolute(target)) return 'Use an absolute path.'
	if (target.split(path.sep).includes('..')) return 'Paths must not contain `..`.'
	const real = realLocation(target)
	if (real === null || !within(ROOT, real)) return `Stay inside ${ROOT}.`
	if ((tool === 'create_file' || tool === 'edit_file') && within(CONFIG, real)) return 'The .amp directory is read-only.'
	return null
}

// The regular files under `start`, by path relative to the snapshot, in a stable order.
// Links are not followed: their targets may be outside the snapshot.
function* files(start) {
	const stat = lstatSync(start)
	if (stat.isFile()) {
		yield path.relative(ROOT, start)
	} else if (stat.isDirectory()) {
		const entries = readdirSync(start, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1))
		for (const entry of entries) {
			if (entry.isDirectory() || entry.isFile()) yield* files(path.join(start, entry.name))
		}
	}
}

// The search's starting point, checked again here so a search never depends on the hook.
function start(tool, input) {
	const message = refusal(tool, input)
	if (message !== null) throw new Error(message)
	return realLocation(input.path ?? ROOT)
}

function matching(input, name) {
	return typeof input.glob !== 'string' || path.matchesGlob(name, input.glob)
}

function report(lines, none) {
	if (lines.length === 0) return none
	const shown = lines.slice(0, LIMIT).join('\n')
	return lines.length > LIMIT ? `${shown}\nMore results; narrow the search.` : shown
}

function searchFiles(input) {
	const from = start('search_files', input)
	let regex
	try {
		regex = new RegExp(String(input.pattern), input.ignore_case ? 'i' : '')
	} catch (error) {
		throw new Error(`Invalid pattern: ${error.message}`)
	}
	const lines = []
	for (const name of files(from)) {
		if (!matching(input, name)) continue
		const data = readFileSync(path.join(ROOT, name))
		if (data.length > MAX_FILE || data.subarray(0, 8192).includes(0)) continue
		const text = data.toString('utf8').split('\n')
		for (let index = 0; index < text.length && lines.length <= LIMIT; index++) {
			if (regex.test(text[index])) lines.push(`${name}:${index + 1}: ${text[index].slice(0, 300)}`)
		}
		if (lines.length > LIMIT) break
	}
	return report(lines, 'No matches.')
}

function findFiles(input) {
	const lines = []
	for (const name of files(start('find_files', input))) {
		if (matching(input, name)) lines.push(name)
		if (lines.length > LIMIT) break
	}
	return report(lines, 'No files.')
}

const PATH = { type: 'string', description: 'Absolute file or directory to search; defaults to the working directory.' }
const GLOB = {
	type: 'string',
	description: 'Only paths, relative to the working directory, matching this glob, such as `**/*.py` or `src/*.rs`.',
}

export default function (amp) {
	amp.registerTool({
		name: 'search_files',
		description: `Search file contents for a JavaScript regular expression, line by line. Returns up to ${LIMIT} matches as path:line: text. Skips binary files and links.`,
		inputSchema: {
			type: 'object',
			properties: {
				pattern: { type: 'string', description: 'The regular expression.' },
				path: PATH,
				glob: GLOB,
				ignore_case: { type: 'boolean' },
			},
			required: ['pattern'],
		},
		async execute(input) {
			return searchFiles(input)
		},
	})
	amp.registerTool({
		name: 'find_files',
		description: `List files whose path matches a glob. Returns up to ${LIMIT} paths relative to the working directory.`,
		inputSchema: { type: 'object', properties: { glob: GLOB, path: PATH }, required: ['glob'] },
		async execute(input) {
			return findFiles(input)
		},
	})
	const agent = amp.createAgent({
		model: __MODEL__,
		tools: { include: [...TOOLS], exclude: ['tool_search', 'code_exec'] },__EFFORT__
		instructions: SHELL
			? 'You are a reviewer. You can read, search and change files in your working directory and run commands in a sandbox.'
			: 'You are a reviewer. You can read, search and change files in your working directory but cannot run commands.',
	})
	amp.registerAgentMode({ key: '__MODE__', label: '__MODE__', agent: agent.definition })
	amp.on('tool.call', async (event) => {
		let message
		try {
			message = refusal(event.tool, event.input)
		} catch {
			message = 'The path could not be checked.'
		}
		return message === null ? { action: 'allow' } : { action: 'reject-and-continue', message }
	})
}
