import { canonical, parseStrictJson, validateType, encodeRequest, decodeResponse, supportsSchema } from '../../generated/api/api_3_0.ts';
import type { Json, Types, ValidatedConnect } from '../../generated/api/api_3_0.ts';
declare const process: { argv: string[] };
function obj(v: Json): { [key: string]: Json } { if (typeof v !== 'object' || v === null || Array.isArray(v)) throw Error('object'); return v; }
function str(v: Json): string { if (typeof v !== 'string') throw Error('string'); return v; }
function unhex(s: string): Uint8Array { if (!/^(?:[0-9a-f]{2})*$/.test(s)) throw Error('hex'); return new Uint8Array(s.match(/../g)?.map(x => parseInt(x, 16)) ?? []); }
async function run(value: Json): Promise<unknown> {
    const v = obj(value), action = str(v['action']);
    if (action === 'parse') { try { parseStrictJson(str(v['text'])); return { valid: true }; } catch { return { valid: false }; } }
    if (action === 'schema') return { valid: supportsSchema(str(v['schema'])) };
    if (action === 'validate') return { valid: await validateType(str(v['type']) as keyof Types, v['value']) };
    if (action === 'encode') { const bytes = await encodeRequest(str(v['operation']), str(v['id']), v['value']); return { hex: Array.from(bytes).map(x => x.toString(16).padStart(2, '0')).join('') }; }
    if (action === 'encode_error') {
        if (!await validateType('ApiError', v['value'])) return { invalid: true };
        const body = new TextEncoder().encode(canonical({ request_id: str(v['id']), error: v['value'] }));
        const bytes = new Uint8Array(body.length + 4);
        new DataView(bytes.buffer).setUint32(0, body.length, false); bytes.set(body, 4);
        return { hex: Array.from(bytes).map(x => x.toString(16).padStart(2, '0')).join('') };
    }
    if (action === 'decode') {
        let context: ValidatedConnect | undefined;
        if (v['connect'] !== '') { const result = await decodeResponse(unhex(str(v['connect'])), 'protocol.connect', '1'); if (result.kind === 'connect') context = result.context; }
        const result = await decodeResponse(unhex(str(v['frame'])), str(v['operation']), str(v['id']), context);
        if (result.kind === 'unavailable') return { unavailable: true };
        if (result.kind === 'error') return { error: result.error };
        if (result.kind !== 'schema') return { result: result.result };
        return { result: { protocol: result.result.protocol, schema_digest: result.result.schema_digest, schema_byte_count: result.result.schema_byte_count } };
    }
    throw Error('action');
}
async function main(): Promise<void> { const moduleName: string = 'node:fs'; const fs: { readFileSync(path: string, encoding: string): string } = await import(moduleName); for (const line of fs.readFileSync(process.argv[2], 'utf8').trimEnd().split('\n')) console.log(canonical(await run(parseStrictJson(line)))); }
void main();
