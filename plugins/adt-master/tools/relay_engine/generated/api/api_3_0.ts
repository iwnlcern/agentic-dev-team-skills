export type AcceptedOlderKind = "accepted_older";
export type AcceptedOlderNotice = {readonly "kind": AcceptedOlderKind;readonly "negotiated": ProtocolVersion;readonly "newest": ProtocolVersion;readonly "remedy": UpdateClientRemedy};
export type ApiCauseAlreadyConnected = {readonly "kind": ApiKindAlreadyConnected};
export type ApiCauseHandlerFault = {readonly "kind": ApiKindHandlerFault};
export type ApiCauseHandshakeRequired = {readonly "kind": ApiKindHandshakeRequired};
export type ApiCauseInvalidArgs = {readonly "kind": ApiKindInvalidArgs};
export type ApiCauseInvalidEnvelope = {readonly "kind": ApiKindInvalidEnvelope};
export type ApiCauseMalformedFrame = {readonly "kind": ApiKindMalformedFrame};
export type ApiCauseMalformedJson = {readonly "kind": ApiKindMalformedJson};
export type ApiCauseNonIncreasingRequestId = {readonly "kind": ApiKindNonIncreasingRequestId};
export type ApiCausePinMismatch = {readonly "kind": ApiKindPinMismatch;readonly "field": ApiPinField;readonly "required": Digest;readonly "actual": Digest};
export type ApiCauseProtocolWindow = {readonly "kind": ApiKindProtocolWindow;readonly "requested": ProtocolWindow;readonly "supported": ProtocolWindow};
export type ApiCauseRuntimeNotReady = {readonly "kind": ApiKindRuntimeNotReady};
export type ApiCauseSchemaUnavailable = {readonly "kind": ApiKindSchemaUnavailable};
export type ApiCauseUnknownOperation = {readonly "kind": ApiKindUnknownOperation};
export type ApiError = __ApiError0 | __ApiError1 | __ApiError2 | __ApiError3 | __ApiError4 | __ApiError5 | __ApiError6 | __ApiError7 | __ApiError8 | __ApiError9 | __ApiError10 | __ApiError11 | __ApiError12;
export type ApiIncompatibleCause = ApiCausePinMismatch | ApiCauseProtocolWindow;
export type ApiInternalCause = ApiCauseHandlerFault;
export type ApiKindAlreadyConnected = "already_connected";
export type ApiKindHandlerFault = "handler_fault";
export type ApiKindHandshakeRequired = "handshake_required";
export type ApiKindInvalidArgs = "invalid_args";
export type ApiKindInvalidEnvelope = "invalid_envelope";
export type ApiKindMalformedFrame = "malformed_frame";
export type ApiKindMalformedJson = "malformed_json";
export type ApiKindNonIncreasingRequestId = "non_increasing_request_id";
export type ApiKindPinMismatch = "pin_mismatch";
export type ApiKindProtocolWindow = "protocol_window";
export type ApiKindRuntimeNotReady = "runtime_not_ready";
export type ApiKindSchemaUnavailable = "schema_unavailable";
export type ApiKindUnknownOperation = "unknown_operation";
export type ApiPinField = "build_id" | "schema_digest";
export type ApiProtocolCause = ApiCauseInvalidArgs | ApiCauseInvalidEnvelope | ApiCauseMalformedFrame | ApiCauseMalformedJson | ApiCauseUnknownOperation;
export type ApiStateCause = ApiCauseAlreadyConnected | ApiCauseHandshakeRequired | ApiCauseNonIncreasingRequestId;
export type ApiUnavailableCause = ApiCauseRuntimeNotReady | ApiCauseSchemaUnavailable;
export type CanonicalSchemaBase64 = string;
export type Capabilities = ReadonlyArray<Capability>;
export type Capability = {readonly "operation": OperationName};
export type ConnectArgs = {readonly "supported": ProtocolWindow;readonly "required_build_id"?: Digest;readonly "required_schema_digest"?: Digest};
export type ConnectResult = {readonly "protocol": ProtocolVersion;readonly "engine_version": EngineVersion;readonly "build_id": Digest;readonly "schema_digest": Digest;readonly "store_id": OpaqueId;readonly "store_kind": StoreKind;readonly "runtime_instance_id": OpaqueId;readonly "supported": ProtocolWindow;readonly "capabilities": Capabilities;readonly "deprecation": DeprecationNotice};
export type DeprecationNotice = AcceptedOlderNotice | null;
export type Digest = string;
export type EngineVersion = string;
export type OpaqueId = string;
export type OperationName = string;
export type ProtocolVersion = string;
export type ProtocolWindow = {readonly "min": ProtocolVersion;readonly "max": ProtocolVersion};
export type RequestId = string;
export type SchemaArgs = {};
export type SchemaEncoding = "base64";
export type SchemaResult = {readonly "protocol": ProtocolVersion;readonly "schema_digest": Digest;readonly "schema_encoding": SchemaEncoding;readonly "schema_byte_count": UInt64;readonly "schema_bytes_base64": CanonicalSchemaBase64};
export type StoreKind = "production" | "isolated";
export type UInt64 = string;
export type UpdateClientRemedy = "update_client";
export type __ApiError0 = {readonly "code": __ApiError0Code;readonly "class": __ApiError0Class;readonly "cause": ApiCausePinMismatch;readonly "remedy": __ApiError0Remedy};
export type __ApiError0Class = "version";
export type __ApiError0Code = "E_API_INCOMPATIBLE";
export type __ApiError0Remedy = "select_matching_candidate";
export type __ApiError1 = {readonly "code": __ApiError1Code;readonly "class": __ApiError1Class;readonly "cause": ApiCauseProtocolWindow;readonly "remedy": __ApiError1Remedy};
export type __ApiError10 = {readonly "code": __ApiError10Code;readonly "class": __ApiError10Class;readonly "cause": ApiCauseNonIncreasingRequestId;readonly "remedy": __ApiError10Remedy};
export type __ApiError10Class = "wire";
export type __ApiError10Code = "E_API_STATE";
export type __ApiError10Remedy = "reconnect_explicitly";
export type __ApiError11 = {readonly "code": __ApiError11Code;readonly "class": __ApiError11Class;readonly "cause": ApiCauseRuntimeNotReady;readonly "remedy": __ApiError11Remedy};
export type __ApiError11Class = "unavailable";
export type __ApiError11Code = "E_API_UNAVAILABLE";
export type __ApiError11Remedy = "restore_expected_runtime";
export type __ApiError12 = {readonly "code": __ApiError12Code;readonly "class": __ApiError12Class;readonly "cause": ApiCauseSchemaUnavailable;readonly "remedy": __ApiError12Remedy};
export type __ApiError12Class = "unavailable";
export type __ApiError12Code = "E_API_UNAVAILABLE";
export type __ApiError12Remedy = "restore_expected_runtime";
export type __ApiError1Class = "version";
export type __ApiError1Code = "E_API_INCOMPATIBLE";
export type __ApiError1Remedy = "update_client";
export type __ApiError2 = {readonly "code": __ApiError2Code;readonly "class": __ApiError2Class;readonly "cause": ApiCauseHandlerFault;readonly "remedy": __ApiError2Remedy};
export type __ApiError2Class = "internal";
export type __ApiError2Code = "E_API_INTERNAL";
export type __ApiError2Remedy = "report_nonsecret_failure";
export type __ApiError3 = {readonly "code": __ApiError3Code;readonly "class": __ApiError3Class;readonly "cause": ApiCauseInvalidArgs;readonly "remedy": __ApiError3Remedy};
export type __ApiError3Class = "wire";
export type __ApiError3Code = "E_API_PROTOCOL";
export type __ApiError3Remedy = "resend_valid_request";
export type __ApiError4 = {readonly "code": __ApiError4Code;readonly "class": __ApiError4Class;readonly "cause": ApiCauseInvalidEnvelope;readonly "remedy": __ApiError4Remedy};
export type __ApiError4Class = "wire";
export type __ApiError4Code = "E_API_PROTOCOL";
export type __ApiError4Remedy = "resend_valid_request";
export type __ApiError5 = {readonly "code": __ApiError5Code;readonly "class": __ApiError5Class;readonly "cause": ApiCauseMalformedFrame;readonly "remedy": __ApiError5Remedy};
export type __ApiError5Class = "wire";
export type __ApiError5Code = "E_API_PROTOCOL";
export type __ApiError5Remedy = "resend_valid_request";
export type __ApiError6 = {readonly "code": __ApiError6Code;readonly "class": __ApiError6Class;readonly "cause": ApiCauseMalformedJson;readonly "remedy": __ApiError6Remedy};
export type __ApiError6Class = "wire";
export type __ApiError6Code = "E_API_PROTOCOL";
export type __ApiError6Remedy = "resend_valid_request";
export type __ApiError7 = {readonly "code": __ApiError7Code;readonly "class": __ApiError7Class;readonly "cause": ApiCauseUnknownOperation;readonly "remedy": __ApiError7Remedy};
export type __ApiError7Class = "wire";
export type __ApiError7Code = "E_API_PROTOCOL";
export type __ApiError7Remedy = "refresh_schema";
export type __ApiError8 = {readonly "code": __ApiError8Code;readonly "class": __ApiError8Class;readonly "cause": ApiCauseAlreadyConnected;readonly "remedy": __ApiError8Remedy};
export type __ApiError8Class = "wire";
export type __ApiError8Code = "E_API_STATE";
export type __ApiError8Remedy = "reconnect_explicitly";
export type __ApiError9 = {readonly "code": __ApiError9Code;readonly "class": __ApiError9Class;readonly "cause": ApiCauseHandshakeRequired;readonly "remedy": __ApiError9Remedy};
export type __ApiError9Class = "wire";
export type __ApiError9Code = "E_API_STATE";
export type __ApiError9Remedy = "reconnect_explicitly";
export type Types = {"AcceptedOlderKind": AcceptedOlderKind;"AcceptedOlderNotice": AcceptedOlderNotice;"ApiCauseAlreadyConnected": ApiCauseAlreadyConnected;"ApiCauseHandlerFault": ApiCauseHandlerFault;"ApiCauseHandshakeRequired": ApiCauseHandshakeRequired;"ApiCauseInvalidArgs": ApiCauseInvalidArgs;"ApiCauseInvalidEnvelope": ApiCauseInvalidEnvelope;"ApiCauseMalformedFrame": ApiCauseMalformedFrame;"ApiCauseMalformedJson": ApiCauseMalformedJson;"ApiCauseNonIncreasingRequestId": ApiCauseNonIncreasingRequestId;"ApiCausePinMismatch": ApiCausePinMismatch;"ApiCauseProtocolWindow": ApiCauseProtocolWindow;"ApiCauseRuntimeNotReady": ApiCauseRuntimeNotReady;"ApiCauseSchemaUnavailable": ApiCauseSchemaUnavailable;"ApiCauseUnknownOperation": ApiCauseUnknownOperation;"ApiError": ApiError;"ApiIncompatibleCause": ApiIncompatibleCause;"ApiInternalCause": ApiInternalCause;"ApiKindAlreadyConnected": ApiKindAlreadyConnected;"ApiKindHandlerFault": ApiKindHandlerFault;"ApiKindHandshakeRequired": ApiKindHandshakeRequired;"ApiKindInvalidArgs": ApiKindInvalidArgs;"ApiKindInvalidEnvelope": ApiKindInvalidEnvelope;"ApiKindMalformedFrame": ApiKindMalformedFrame;"ApiKindMalformedJson": ApiKindMalformedJson;"ApiKindNonIncreasingRequestId": ApiKindNonIncreasingRequestId;"ApiKindPinMismatch": ApiKindPinMismatch;"ApiKindProtocolWindow": ApiKindProtocolWindow;"ApiKindRuntimeNotReady": ApiKindRuntimeNotReady;"ApiKindSchemaUnavailable": ApiKindSchemaUnavailable;"ApiKindUnknownOperation": ApiKindUnknownOperation;"ApiPinField": ApiPinField;"ApiProtocolCause": ApiProtocolCause;"ApiStateCause": ApiStateCause;"ApiUnavailableCause": ApiUnavailableCause;"CanonicalSchemaBase64": CanonicalSchemaBase64;"Capabilities": Capabilities;"Capability": Capability;"ConnectArgs": ConnectArgs;"ConnectResult": ConnectResult;"DeprecationNotice": DeprecationNotice;"Digest": Digest;"EngineVersion": EngineVersion;"OpaqueId": OpaqueId;"OperationName": OperationName;"ProtocolVersion": ProtocolVersion;"ProtocolWindow": ProtocolWindow;"RequestId": RequestId;"SchemaArgs": SchemaArgs;"SchemaEncoding": SchemaEncoding;"SchemaResult": SchemaResult;"StoreKind": StoreKind;"UInt64": UInt64;"UpdateClientRemedy": UpdateClientRemedy;"__ApiError0": __ApiError0;"__ApiError0Class": __ApiError0Class;"__ApiError0Code": __ApiError0Code;"__ApiError0Remedy": __ApiError0Remedy;"__ApiError1": __ApiError1;"__ApiError10": __ApiError10;"__ApiError10Class": __ApiError10Class;"__ApiError10Code": __ApiError10Code;"__ApiError10Remedy": __ApiError10Remedy;"__ApiError11": __ApiError11;"__ApiError11Class": __ApiError11Class;"__ApiError11Code": __ApiError11Code;"__ApiError11Remedy": __ApiError11Remedy;"__ApiError12": __ApiError12;"__ApiError12Class": __ApiError12Class;"__ApiError12Code": __ApiError12Code;"__ApiError12Remedy": __ApiError12Remedy;"__ApiError1Class": __ApiError1Class;"__ApiError1Code": __ApiError1Code;"__ApiError1Remedy": __ApiError1Remedy;"__ApiError2": __ApiError2;"__ApiError2Class": __ApiError2Class;"__ApiError2Code": __ApiError2Code;"__ApiError2Remedy": __ApiError2Remedy;"__ApiError3": __ApiError3;"__ApiError3Class": __ApiError3Class;"__ApiError3Code": __ApiError3Code;"__ApiError3Remedy": __ApiError3Remedy;"__ApiError4": __ApiError4;"__ApiError4Class": __ApiError4Class;"__ApiError4Code": __ApiError4Code;"__ApiError4Remedy": __ApiError4Remedy;"__ApiError5": __ApiError5;"__ApiError5Class": __ApiError5Class;"__ApiError5Code": __ApiError5Code;"__ApiError5Remedy": __ApiError5Remedy;"__ApiError6": __ApiError6;"__ApiError6Class": __ApiError6Class;"__ApiError6Code": __ApiError6Code;"__ApiError6Remedy": __ApiError6Remedy;"__ApiError7": __ApiError7;"__ApiError7Class": __ApiError7Class;"__ApiError7Code": __ApiError7Code;"__ApiError7Remedy": __ApiError7Remedy;"__ApiError8": __ApiError8;"__ApiError8Class": __ApiError8Class;"__ApiError8Code": __ApiError8Code;"__ApiError8Remedy": __ApiError8Remedy;"__ApiError9": __ApiError9;"__ApiError9Class": __ApiError9Class;"__ApiError9Code": __ApiError9Code;"__ApiError9Remedy": __ApiError9Remedy};

// Generated by tools/generate-api.py. Runtime validation precedes every typed adoption.
export type Json = null | boolean | string | Json[] | { [key: string]: Json };
type Obj = { [key: string]: unknown };
function object(v: unknown): v is Obj { return typeof v === 'object' && v !== null && !Array.isArray(v) && (Object.getPrototypeOf(v) === null || Object.getPrototypeOf(v) === Object.prototype); }
function record(v: unknown): Obj { if (!object(v)) throw Error('protocol_unavailable'); return v; }
function string(v: unknown): string { if (typeof v !== 'string') throw Error('protocol_unavailable'); return v; }
function scalar(s: string): boolean { for (const c of s) { const n = c.codePointAt(0)!; if (n >= 0xd800 && n <= 0xdfff) return false; } return true; }
function parse(text: string, numbers: boolean): unknown {
    let at = 0;
    const ws = (): void => { while (at < text.length && ' \t\r\n'.includes(text[at])) at++; };
    const take = (c: string): void => { if (text[at++] !== c) throw Error('invalid_json'); };
    const quoted = (): string => {
        const start = at; take('"');
        while (at < text.length) { const c = text[at++]; if (c === '"') { const value: unknown = JSON.parse(text.slice(start, at)); const s = string(value); if (!scalar(s)) throw Error('invalid_unicode'); return s; } if (c === '\\') at++; }
        throw Error('invalid_json');
    };
    const value = (depth: number): unknown => {
        if (depth > 64) throw Error('depth'); ws();
        const c = text[at];
        if (c === '"') return quoted();
        if (c === '{') { at++; ws(); const out: Obj = Object.create(null); if (text[at] === '}') { at++; return out; } while (true) { ws(); const key = quoted(); if (Object.hasOwn(out, key)) throw Error('duplicate_key'); ws(); take(':'); out[key] = value(depth + 1); ws(); if (text[at] === '}') { at++; return out; } take(','); } }
        if (c === '[') { at++; ws(); const out: unknown[] = []; if (text[at] === ']') { at++; return out; } while (true) { out.push(value(depth + 1)); ws(); if (text[at] === ']') { at++; return out; } take(','); } }
        for (const [word, result] of [['true', true], ['false', false], ['null', null]] as const) if (text.startsWith(word, at)) { at += word.length; return result; }
        if (numbers) { const m = /^(?:0|[1-9][0-9]*)/.exec(text.slice(at)); if (m) { at += m[0].length; const n = Number(m[0]); if (!Number.isSafeInteger(n)) throw Error('number'); return n; } }
        throw Error('invalid_json');
    };
    const result = value(0); ws(); if (at !== text.length) throw Error('trailing_data'); return result;
}
function wire(value: unknown, depth = 0): value is Json {
    if (depth > 64) return false;
    if (value === null || typeof value === 'boolean') return true;
    if (typeof value === 'string') return scalar(value);
    if (Array.isArray(value)) return Object.keys(value).length === value.length && value.every(x => wire(x, depth + 1));
    if (!object(value)) return false;
    return Reflect.ownKeys(value).length === Object.keys(value).length && Object.entries(Object.getOwnPropertyDescriptors(value)).every(([key, desc]) => scalar(key) && 'value' in desc && desc.enumerable && wire(desc.value, depth + 1));
}
export function parseStrictJson(text: string): Json { const v = parse(text, false); if (!wire(v)) throw Error('invalid_json'); return v; }
export function canonical(value: unknown): string {
    if (value === null || typeof value === 'boolean' || typeof value === 'number') return JSON.stringify(value);
    if (typeof value === 'string') { if (!scalar(value)) throw Error('invalid_unicode'); return JSON.stringify(value); }
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    const o = record(value); return '{' + Object.keys(o).sort().map(k => JSON.stringify(k) + ':' + canonical(o[k])).join(',') + '}';
}
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function encodeBase64(raw: Uint8Array): string {
    const parts: string[] = []; for (let i = 0; i < raw.length; i += 3) { const a = raw[i], b = raw[i + 1] ?? 0, c = raw[i + 2] ?? 0; parts.push(alphabet[a >> 2] + alphabet[(a & 3) * 16 + (b >> 4)] + (i + 1 < raw.length ? alphabet[(b & 15) * 4 + (c >> 6)] : '=') + (i + 2 < raw.length ? alphabet[c & 63] : '=')); } return parts.join('');
}
function decodeBase64(s: string): Uint8Array {
    if (s.length > 11184812 || s.length % 4 !== 0) throw Error('base64');
    const out: number[] = []; for (let i = 0; i < s.length; i += 4) { let n = 0; for (let j = 0; j < 4; j++) { const c = s[i + j], digit = c === '=' ? 0 : alphabet.indexOf(c); if (digit < 0) throw Error('base64'); n = (n << 6) | digit; } out.push((n >>> 16) & 255); if (s[i + 2] !== '=') out.push((n >>> 8) & 255); if (s[i + 3] !== '=') out.push(n & 255); }
    const raw = new Uint8Array(out); if (raw.length > 8388608 || encodeBase64(raw) !== s) throw Error('base64'); return raw;
}
async function digest(raw: Uint8Array): Promise<string> { const input = new Uint8Array(raw); return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', input.buffer))).map(x => x.toString(16).padStart(2, '0')).join(''); }
const schema: unknown = {"$defs":{"AcceptedOlderKind":{"enum":["accepted_older"],"type":"string"},"AcceptedOlderNotice":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/AcceptedOlderKind"},"negotiated":{"$ref":"#/$defs/ProtocolVersion"},"newest":{"$ref":"#/$defs/ProtocolVersion"},"remedy":{"$ref":"#/$defs/UpdateClientRemedy"}},"required":["kind","negotiated","newest","remedy"],"type":"object"},"ApiCauseAlreadyConnected":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindAlreadyConnected"}},"required":["kind"],"type":"object"},"ApiCauseHandlerFault":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindHandlerFault"}},"required":["kind"],"type":"object"},"ApiCauseHandshakeRequired":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindHandshakeRequired"}},"required":["kind"],"type":"object"},"ApiCauseInvalidArgs":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindInvalidArgs"}},"required":["kind"],"type":"object"},"ApiCauseInvalidEnvelope":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindInvalidEnvelope"}},"required":["kind"],"type":"object"},"ApiCauseMalformedFrame":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindMalformedFrame"}},"required":["kind"],"type":"object"},"ApiCauseMalformedJson":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindMalformedJson"}},"required":["kind"],"type":"object"},"ApiCauseNonIncreasingRequestId":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindNonIncreasingRequestId"}},"required":["kind"],"type":"object"},"ApiCausePinMismatch":{"additionalProperties":false,"properties":{"actual":{"$ref":"#/$defs/Digest"},"field":{"$ref":"#/$defs/ApiPinField"},"kind":{"$ref":"#/$defs/ApiKindPinMismatch"},"required":{"$ref":"#/$defs/Digest"}},"required":["kind","field","required","actual"],"type":"object"},"ApiCauseProtocolWindow":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindProtocolWindow"},"requested":{"$ref":"#/$defs/ProtocolWindow"},"supported":{"$ref":"#/$defs/ProtocolWindow"}},"required":["kind","requested","supported"],"type":"object"},"ApiCauseRuntimeNotReady":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindRuntimeNotReady"}},"required":["kind"],"type":"object"},"ApiCauseSchemaUnavailable":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindSchemaUnavailable"}},"required":["kind"],"type":"object"},"ApiCauseUnknownOperation":{"additionalProperties":false,"properties":{"kind":{"$ref":"#/$defs/ApiKindUnknownOperation"}},"required":["kind"],"type":"object"},"ApiError":{"oneOf":[{"$ref":"#/$defs/__ApiError0"},{"$ref":"#/$defs/__ApiError1"},{"$ref":"#/$defs/__ApiError2"},{"$ref":"#/$defs/__ApiError3"},{"$ref":"#/$defs/__ApiError4"},{"$ref":"#/$defs/__ApiError5"},{"$ref":"#/$defs/__ApiError6"},{"$ref":"#/$defs/__ApiError7"},{"$ref":"#/$defs/__ApiError8"},{"$ref":"#/$defs/__ApiError9"},{"$ref":"#/$defs/__ApiError10"},{"$ref":"#/$defs/__ApiError11"},{"$ref":"#/$defs/__ApiError12"}]},"ApiFailure":{"additionalProperties":false,"properties":{"error":{"$ref":"#/$defs/ApiError"},"request_id":{"oneOf":[{"$ref":"#/$defs/RequestId"},{"type":"null"}]}},"required":["request_id","error"],"type":"object"},"ApiIncompatibleCause":{"oneOf":[{"$ref":"#/$defs/ApiCausePinMismatch"},{"$ref":"#/$defs/ApiCauseProtocolWindow"}]},"ApiInternalCause":{"oneOf":[{"$ref":"#/$defs/ApiCauseHandlerFault"}]},"ApiKindAlreadyConnected":{"enum":["already_connected"],"type":"string"},"ApiKindHandlerFault":{"enum":["handler_fault"],"type":"string"},"ApiKindHandshakeRequired":{"enum":["handshake_required"],"type":"string"},"ApiKindInvalidArgs":{"enum":["invalid_args"],"type":"string"},"ApiKindInvalidEnvelope":{"enum":["invalid_envelope"],"type":"string"},"ApiKindMalformedFrame":{"enum":["malformed_frame"],"type":"string"},"ApiKindMalformedJson":{"enum":["malformed_json"],"type":"string"},"ApiKindNonIncreasingRequestId":{"enum":["non_increasing_request_id"],"type":"string"},"ApiKindPinMismatch":{"enum":["pin_mismatch"],"type":"string"},"ApiKindProtocolWindow":{"enum":["protocol_window"],"type":"string"},"ApiKindRuntimeNotReady":{"enum":["runtime_not_ready"],"type":"string"},"ApiKindSchemaUnavailable":{"enum":["schema_unavailable"],"type":"string"},"ApiKindUnknownOperation":{"enum":["unknown_operation"],"type":"string"},"ApiPinField":{"enum":["build_id","schema_digest"],"type":"string"},"ApiProtocolCause":{"oneOf":[{"$ref":"#/$defs/ApiCauseInvalidArgs"},{"$ref":"#/$defs/ApiCauseInvalidEnvelope"},{"$ref":"#/$defs/ApiCauseMalformedFrame"},{"$ref":"#/$defs/ApiCauseMalformedJson"},{"$ref":"#/$defs/ApiCauseUnknownOperation"}]},"ApiRequest":{"oneOf":[{"$ref":"#/$defs/Request_protocol_connect"},{"$ref":"#/$defs/Request_protocol_schema"}]},"ApiResponse":{"oneOf":[{"$ref":"#/$defs/Success_protocol_connect"},{"$ref":"#/$defs/Success_protocol_schema"},{"$ref":"#/$defs/ApiFailure"}]},"ApiStateCause":{"oneOf":[{"$ref":"#/$defs/ApiCauseAlreadyConnected"},{"$ref":"#/$defs/ApiCauseHandshakeRequired"},{"$ref":"#/$defs/ApiCauseNonIncreasingRequestId"}]},"ApiUnavailableCause":{"oneOf":[{"$ref":"#/$defs/ApiCauseRuntimeNotReady"},{"$ref":"#/$defs/ApiCauseSchemaUnavailable"}]},"CanonicalSchemaBase64":{"maxLength":11184812,"minLength":0,"pattern":"^(?:(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?)$(?![\\s\\S])","type":"string"},"Capabilities":{"items":{"$ref":"#/$defs/Capability"},"maxItems":4096,"type":"array"},"Capability":{"additionalProperties":false,"properties":{"operation":{"$ref":"#/$defs/OperationName"}},"required":["operation"],"type":"object"},"ConnectArgs":{"additionalProperties":false,"properties":{"required_build_id":{"$ref":"#/$defs/Digest"},"required_schema_digest":{"$ref":"#/$defs/Digest"},"supported":{"$ref":"#/$defs/ProtocolWindow"}},"required":["supported"],"type":"object"},"ConnectResult":{"additionalProperties":false,"properties":{"build_id":{"$ref":"#/$defs/Digest"},"capabilities":{"$ref":"#/$defs/Capabilities"},"deprecation":{"$ref":"#/$defs/DeprecationNotice"},"engine_version":{"$ref":"#/$defs/EngineVersion"},"protocol":{"$ref":"#/$defs/ProtocolVersion"},"runtime_instance_id":{"$ref":"#/$defs/OpaqueId"},"schema_digest":{"$ref":"#/$defs/Digest"},"store_id":{"$ref":"#/$defs/OpaqueId"},"store_kind":{"$ref":"#/$defs/StoreKind"},"supported":{"$ref":"#/$defs/ProtocolWindow"}},"required":["protocol","engine_version","build_id","schema_digest","store_id","store_kind","runtime_instance_id","supported","capabilities","deprecation"],"type":"object"},"DeprecationNotice":{"oneOf":[{"type":"null"},{"$ref":"#/$defs/AcceptedOlderNotice"}]},"Digest":{"maxLength":64,"minLength":64,"pattern":"^(?:[0-9a-f]{64})$(?![\\s\\S])","type":"string"},"EngineVersion":{"maxLength":17,"minLength":5,"pattern":"^(?:(?:0|[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])\\.(?:0|[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])\\.(?:0|[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))$(?![\\s\\S])","type":"string"},"OpaqueId":{"maxLength":256,"minLength":1,"pattern":"^(?:[^\\x00]+)$(?![\\s\\S])","type":"string"},"OperationName":{"maxLength":96,"minLength":1,"pattern":"^(?:[a-z][a-z0-9.\\-]{0,95})$(?![\\s\\S])","type":"string"},"ProtocolVersion":{"maxLength":11,"minLength":3,"pattern":"^(?:(?:0|[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])\\.(?:0|[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))$(?![\\s\\S])","type":"string"},"ProtocolWindow":{"additionalProperties":false,"properties":{"max":{"$ref":"#/$defs/ProtocolVersion"},"min":{"$ref":"#/$defs/ProtocolVersion"}},"required":["min","max"],"type":"object"},"RequestId":{"maxLength":20,"minLength":1,"pattern":"^(?:[1-9][0-9]{0,19})$(?![\\s\\S])","type":"string"},"Request_protocol_connect":{"additionalProperties":false,"properties":{"args":{"$ref":"#/$defs/ConnectArgs"},"op":{"const":"protocol.connect"},"request_id":{"$ref":"#/$defs/RequestId"}},"required":["request_id","op","args"],"type":"object"},"Request_protocol_schema":{"additionalProperties":false,"properties":{"args":{"$ref":"#/$defs/SchemaArgs"},"op":{"const":"protocol.schema"},"request_id":{"$ref":"#/$defs/RequestId"}},"required":["request_id","op","args"],"type":"object"},"SchemaArgs":{"additionalProperties":false,"properties":{},"required":[],"type":"object"},"SchemaEncoding":{"enum":["base64"],"type":"string"},"SchemaResult":{"additionalProperties":false,"properties":{"protocol":{"$ref":"#/$defs/ProtocolVersion"},"schema_byte_count":{"$ref":"#/$defs/UInt64"},"schema_bytes_base64":{"$ref":"#/$defs/CanonicalSchemaBase64"},"schema_digest":{"$ref":"#/$defs/Digest"},"schema_encoding":{"$ref":"#/$defs/SchemaEncoding"}},"required":["protocol","schema_digest","schema_encoding","schema_byte_count","schema_bytes_base64"],"type":"object"},"StoreKind":{"enum":["production","isolated"],"type":"string"},"Success_protocol_connect":{"additionalProperties":false,"properties":{"request_id":{"$ref":"#/$defs/RequestId"},"result":{"$ref":"#/$defs/ConnectResult"}},"required":["request_id","result"],"type":"object"},"Success_protocol_schema":{"additionalProperties":false,"properties":{"request_id":{"$ref":"#/$defs/RequestId"},"result":{"$ref":"#/$defs/SchemaResult"}},"required":["request_id","result"],"type":"object"},"UInt64":{"maxLength":20,"minLength":1,"pattern":"^(?:(?:0|[1-9][0-9]{0,19}))$(?![\\s\\S])","type":"string"},"UpdateClientRemedy":{"enum":["update_client"],"type":"string"},"__ApiError0":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCausePinMismatch"},"class":{"$ref":"#/$defs/__ApiError0Class"},"code":{"$ref":"#/$defs/__ApiError0Code"},"remedy":{"$ref":"#/$defs/__ApiError0Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError0Class":{"enum":["version"],"type":"string"},"__ApiError0Code":{"enum":["E_API_INCOMPATIBLE"],"type":"string"},"__ApiError0Remedy":{"enum":["select_matching_candidate"],"type":"string"},"__ApiError1":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseProtocolWindow"},"class":{"$ref":"#/$defs/__ApiError1Class"},"code":{"$ref":"#/$defs/__ApiError1Code"},"remedy":{"$ref":"#/$defs/__ApiError1Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError10":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseNonIncreasingRequestId"},"class":{"$ref":"#/$defs/__ApiError10Class"},"code":{"$ref":"#/$defs/__ApiError10Code"},"remedy":{"$ref":"#/$defs/__ApiError10Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError10Class":{"enum":["wire"],"type":"string"},"__ApiError10Code":{"enum":["E_API_STATE"],"type":"string"},"__ApiError10Remedy":{"enum":["reconnect_explicitly"],"type":"string"},"__ApiError11":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseRuntimeNotReady"},"class":{"$ref":"#/$defs/__ApiError11Class"},"code":{"$ref":"#/$defs/__ApiError11Code"},"remedy":{"$ref":"#/$defs/__ApiError11Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError11Class":{"enum":["unavailable"],"type":"string"},"__ApiError11Code":{"enum":["E_API_UNAVAILABLE"],"type":"string"},"__ApiError11Remedy":{"enum":["restore_expected_runtime"],"type":"string"},"__ApiError12":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseSchemaUnavailable"},"class":{"$ref":"#/$defs/__ApiError12Class"},"code":{"$ref":"#/$defs/__ApiError12Code"},"remedy":{"$ref":"#/$defs/__ApiError12Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError12Class":{"enum":["unavailable"],"type":"string"},"__ApiError12Code":{"enum":["E_API_UNAVAILABLE"],"type":"string"},"__ApiError12Remedy":{"enum":["restore_expected_runtime"],"type":"string"},"__ApiError1Class":{"enum":["version"],"type":"string"},"__ApiError1Code":{"enum":["E_API_INCOMPATIBLE"],"type":"string"},"__ApiError1Remedy":{"enum":["update_client"],"type":"string"},"__ApiError2":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseHandlerFault"},"class":{"$ref":"#/$defs/__ApiError2Class"},"code":{"$ref":"#/$defs/__ApiError2Code"},"remedy":{"$ref":"#/$defs/__ApiError2Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError2Class":{"enum":["internal"],"type":"string"},"__ApiError2Code":{"enum":["E_API_INTERNAL"],"type":"string"},"__ApiError2Remedy":{"enum":["report_nonsecret_failure"],"type":"string"},"__ApiError3":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseInvalidArgs"},"class":{"$ref":"#/$defs/__ApiError3Class"},"code":{"$ref":"#/$defs/__ApiError3Code"},"remedy":{"$ref":"#/$defs/__ApiError3Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError3Class":{"enum":["wire"],"type":"string"},"__ApiError3Code":{"enum":["E_API_PROTOCOL"],"type":"string"},"__ApiError3Remedy":{"enum":["resend_valid_request"],"type":"string"},"__ApiError4":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseInvalidEnvelope"},"class":{"$ref":"#/$defs/__ApiError4Class"},"code":{"$ref":"#/$defs/__ApiError4Code"},"remedy":{"$ref":"#/$defs/__ApiError4Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError4Class":{"enum":["wire"],"type":"string"},"__ApiError4Code":{"enum":["E_API_PROTOCOL"],"type":"string"},"__ApiError4Remedy":{"enum":["resend_valid_request"],"type":"string"},"__ApiError5":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseMalformedFrame"},"class":{"$ref":"#/$defs/__ApiError5Class"},"code":{"$ref":"#/$defs/__ApiError5Code"},"remedy":{"$ref":"#/$defs/__ApiError5Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError5Class":{"enum":["wire"],"type":"string"},"__ApiError5Code":{"enum":["E_API_PROTOCOL"],"type":"string"},"__ApiError5Remedy":{"enum":["resend_valid_request"],"type":"string"},"__ApiError6":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseMalformedJson"},"class":{"$ref":"#/$defs/__ApiError6Class"},"code":{"$ref":"#/$defs/__ApiError6Code"},"remedy":{"$ref":"#/$defs/__ApiError6Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError6Class":{"enum":["wire"],"type":"string"},"__ApiError6Code":{"enum":["E_API_PROTOCOL"],"type":"string"},"__ApiError6Remedy":{"enum":["resend_valid_request"],"type":"string"},"__ApiError7":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseUnknownOperation"},"class":{"$ref":"#/$defs/__ApiError7Class"},"code":{"$ref":"#/$defs/__ApiError7Code"},"remedy":{"$ref":"#/$defs/__ApiError7Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError7Class":{"enum":["wire"],"type":"string"},"__ApiError7Code":{"enum":["E_API_PROTOCOL"],"type":"string"},"__ApiError7Remedy":{"enum":["refresh_schema"],"type":"string"},"__ApiError8":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseAlreadyConnected"},"class":{"$ref":"#/$defs/__ApiError8Class"},"code":{"$ref":"#/$defs/__ApiError8Code"},"remedy":{"$ref":"#/$defs/__ApiError8Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError8Class":{"enum":["wire"],"type":"string"},"__ApiError8Code":{"enum":["E_API_STATE"],"type":"string"},"__ApiError8Remedy":{"enum":["reconnect_explicitly"],"type":"string"},"__ApiError9":{"additionalProperties":false,"properties":{"cause":{"$ref":"#/$defs/ApiCauseHandshakeRequired"},"class":{"$ref":"#/$defs/__ApiError9Class"},"code":{"$ref":"#/$defs/__ApiError9Code"},"remedy":{"$ref":"#/$defs/__ApiError9Remedy"}},"required":["code","class","cause","remedy"],"type":"object"},"__ApiError9Class":{"enum":["wire"],"type":"string"},"__ApiError9Code":{"enum":["E_API_STATE"],"type":"string"},"__ApiError9Remedy":{"enum":["reconnect_explicitly"],"type":"string"}},"$id":"urn:adt:api:3.0","$schema":"https://json-schema.org/draft/2020-12/schema","oneOf":[{"$ref":"#/$defs/ApiRequest"},{"$ref":"#/$defs/ApiResponse"}],"x-adt":{"format":"adt-api-v1","operations":[{"browser_get":"connect","effect":"connection_metadata","error_ref":"ApiError","name":"protocol.connect","principal":"unsigned","request_ref":"ConnectArgs","result_ref":"ConnectResult","retry":"read_repeat"},{"browser_get":"schema","effect":"pure_observation","error_ref":"ApiError","name":"protocol.schema","principal":"unsigned","request_ref":"SchemaArgs","result_ref":"SchemaResult","retry":"read_repeat"}],"protocol":"3.0","rules":[{"kind":"canonical_base64","type_ref":"CanonicalSchemaBase64"},{"array_ref":"Capabilities","key":"operation","kind":"sorted_unique"},{"kind":"utf8_max_bytes","limit":256,"type_ref":"OpaqueId"},{"kind":"protocol_window_v1","max_field":"max","min_field":"min","relation":"same_major_and_ordered","type_ref":"ProtocolWindow"},{"kind":"canonical_uint64","type_ref":"RequestId"},{"bytes_field":"schema_bytes_base64","count_field":"schema_byte_count","digest_algorithm":"sha256","digest_field":"schema_digest","encoding":"base64","encoding_field":"schema_encoding","kind":"schema_payload_v1","max_decoded_bytes":8388608,"type_ref":"SchemaResult"},{"kind":"canonical_uint64","type_ref":"UInt64"},{"context_kind":"schema_reply","digest_field":"schema_digest","digest_slot":"negotiated_schema_digest","kind":"negotiated_schema_v1","operation":"protocol.schema","protocol_field":"protocol","protocol_slot":"negotiated_protocol","result_ref":"SchemaResult"}]}};
const document = record(schema), definitions = record(document['$defs']);
const metadata = record(document['x-adt']);
const rawRules = metadata['rules'];
if (!Array.isArray(rawRules)) throw Error('unsupported_schema');
const requiredRules: unknown[] = rawRules;
export function supportsSchema(text: string): boolean {
    try { const rules = record(record(parse(text, true))['x-adt'])['rules']; return Array.isArray(rules) && canonical(rules) === canonical(requiredRules); } catch { return false; }
}
async function validate(name: string, value: unknown, cache: Map<string, Uint8Array>, depth: number): Promise<boolean> {
    if (depth > 64 || !Object.hasOwn(definitions, name)) return false;
    const node = record(definitions[name]);
    const reference = async (ref: unknown, x: unknown): Promise<boolean> => { const text = string(ref); return text.startsWith('#/$defs/') && validate(text.slice(8), x, cache, depth + 1); };
    if (Array.isArray(node['oneOf'])) { let count = 0; for (const branch of node['oneOf']) { const b = record(branch); if (b['type'] === 'null' ? value === null : await reference(b['$ref'], value)) count++; } if (count !== 1) return false; }
    else if (node['type'] === 'string') {
        if (typeof value !== 'string' || !scalar(value)) return false;
        if (Array.isArray(node['enum']) && !node['enum'].includes(value)) return false;
        const count = [...value].length;
        if (typeof node['minLength'] === 'number' && count < node['minLength']) return false;
        if (typeof node['maxLength'] === 'number' && count > node['maxLength']) return false;
        const canonicalBase64 = requiredRules.some(raw => { const rule = record(raw); return rule['kind'] === 'canonical_base64' && rule['type_ref'] === name; });
        if (!canonicalBase64 && typeof node['pattern'] === 'string' && !new RegExp(node['pattern'], 'u').test(value)) return false;
    } else if (node['type'] === 'object') {
        if (!object(value)) return false;
        const properties = record(node['properties']), required = node['required'];
        if (!Array.isArray(required) || Object.keys(value).some(k => !Object.hasOwn(properties, k)) || required.some(k => typeof k !== 'string' || !Object.hasOwn(value, k))) return false;
        for (const [key, item] of Object.entries(value)) if (!await reference(record(properties[key])['$ref'], item)) return false;
    } else if (node['type'] === 'array') {
        if (!Array.isArray(value) || typeof node['maxItems'] !== 'number' || value.length > node['maxItems']) return false;
        for (const item of value) if (!await reference(record(node['items'])['$ref'], item)) return false;
    } else return false;
    for (const raw of requiredRules) {
        const rule = record(raw); if ((rule['type_ref'] ?? rule['array_ref']) !== name) continue;
        const kind = rule['kind'];
        if (kind === 'utf8_max_bytes') { if (encoder.encode(string(value)).length > Number(rule['limit'])) return false; }
        else if (kind === 'canonical_uint64') { if (BigInt(string(value)) > 18446744073709551615n) return false; }
        else if (kind === 'canonical_base64') { const s = string(value); if (!cache.has(s)) cache.set(s, decodeBase64(s)); }
        else if (kind === 'sorted_unique') { if (!Array.isArray(value)) return false; const names = value.map(x => string(record(x)[string(rule['key'])])); if (names.some((x, i) => i > 0 && names[i - 1] >= x)) return false; }
        else if (kind === 'protocol_window_v1') { const o = record(value), a = string(o[string(rule['min_field'])]).split('.').map(Number), b = string(o[string(rule['max_field'])]).split('.').map(Number); if (a[0] !== b[0] || a[0] > b[0] || (a[0] === b[0] && a[1] > b[1])) return false; }
        else if (kind === 'schema_payload_v1') { const o = record(value), raw = cache.get(string(o[string(rule['bytes_field'])])); if (!raw || String(raw.length) !== o[string(rule['count_field'])] || await digest(raw) !== o[string(rule['digest_field'])]) return false; }
        else return false;
    }
    return true;
}
export async function validateType(name: keyof Types, value: unknown): Promise<boolean> { try { return wire(value) && await validate(name, value, new Map(), 0); } catch { return false; } }
declare const contextBrand: unique symbol;
export type ValidatedConnect = Readonly<{ protocol: string; digest: string; [contextBrand]: true }>;
const contexts = new WeakSet<object>();
function connectContext(result: ConnectResult): ValidatedConnect { const ctx = Object.freeze({ protocol: result.protocol, digest: result.schema_digest }); contexts.add(ctx); return ctx as ValidatedConnect; }
export async function validateOperationResult(operation: string, value: unknown, context?: ValidatedConnect): Promise<boolean> {
    try { const operations = metadata['operations']; if (!Array.isArray(operations)) return false; const op = operations.find(x => record(x)['name'] === operation); if (!op || !wire(value) || !await validate(string(record(op)['result_ref']), value, new Map(), 0)) return false;
        for (const raw of requiredRules) { const rule = record(raw); if (rule['operation'] !== operation) continue; if (rule['kind'] !== 'negotiated_schema_v1' || !context || !contexts.has(context)) return false; const o = record(value); if (o[string(rule['protocol_field'])] !== context.protocol || o[string(rule['digest_field'])] !== context.digest) return false; } return true;
    } catch { return false; }
}
export async function encodeRequest(operation: string, id: string, args: unknown): Promise<Uint8Array> {
    const operations = metadata['operations']; if (!Array.isArray(operations)) throw Error('operation'); const op = operations.find(x => record(x)['name'] === operation);
    if (!op || !await validateType('RequestId', id) || !wire(args) || !await validate(string(record(op)['request_ref']), args, new Map(), 0)) throw Error('invalid_request');
    const raw = encoder.encode(canonical({ request_id: id, op: operation, args })); if (raw.length === 0 || raw.length > 16777216) throw Error('frame'); const frame = new Uint8Array(4 + raw.length); new DataView(frame.buffer).setUint32(0, raw.length); frame.set(raw, 4); return frame;
}
export type DecodedResponse = { kind: 'connect'; result: ConnectResult; context: ValidatedConnect } | { kind: 'schema'; result: SchemaResult } | { kind: 'error'; error: ApiError } | { kind: 'unavailable' };
export async function decodeResponse(frame: Uint8Array, operation: string, outstanding: string, context?: ValidatedConnect): Promise<DecodedResponse> {
    try { if (!await validateType('RequestId', outstanding) || frame.length < 5) throw Error('frame'); const n = new DataView(frame.buffer, frame.byteOffset, frame.byteLength).getUint32(0); if (n === 0 || n > 16777216 || frame.length !== n + 4) throw Error('frame'); const value = record(parseStrictJson(decoder.decode(frame.subarray(4)))); if (Object.keys(value).length !== 2 || value['request_id'] !== outstanding) throw Error('correlation');
        if (Object.hasOwn(value, 'error')) { if (!await validateType('ApiError', value['error'])) throw Error('error'); return { kind: 'error', error: value['error'] as ApiError }; }
        const result = value['result']; if (!await validateOperationResult(operation, result, context)) throw Error('result');
        if (operation === 'protocol.connect') { const typed = result as ConnectResult; return { kind: 'connect', result: typed, context: connectContext(typed) }; }
        if (operation === 'protocol.schema') { const typed = result as SchemaResult; if (!supportsSchema(decoder.decode(decodeBase64(typed.schema_bytes_base64)))) throw Error('unsupported_schema'); return { kind: 'schema', result: typed }; }
        throw Error('operation');
    } catch { return { kind: 'unavailable' }; }
}

