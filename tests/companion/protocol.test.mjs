import {test} from 'node:test';
import assert from 'node:assert/strict';
import {parsePair,allowed,publicError} from '../../src/ore/companion_extension/protocol.js';
const pair={pair_id:'a'.repeat(32),token:'b'.repeat(43),server:'https://ore.example',origins:['https://journal.example'],expires_at:Date.now()/1000+300};
test('remote TLS and loopback tunnels are admitted',()=>{assert.equal(parsePair(JSON.stringify(pair)).websocket,'wss://ore.example/v1/companion/socket');assert.equal(parsePair(JSON.stringify({...pair,server:'http://127.0.0.1:8765'})).websocket,'ws://127.0.0.1:8765/v1/companion/socket');});
test('plaintext remote servers and embedded secrets are rejected',()=>{for(const server of ['http://ore.example','https://u:p@ore.example','https://ore.example/?token=secret','https://ore.example/path'])assert.throws(()=>parsePair(JSON.stringify({...pair,server})));});
test('expired or malformed pairing codes are rejected',()=>{assert.throws(()=>parsePair(JSON.stringify({...pair,expires_at:0})));assert.throws(()=>parsePair(JSON.stringify({...pair,token:'bad'})));});
test('origin checks reject lookalikes, credentials and script URLs',()=>{assert(allowed('https://journal.example/paper',pair.origins));for(const url of ['https://journal.example.evil/paper','https://u:p@journal.example/paper','javascript:alert(1)'])assert(!allowed(url,pair.origins));});
test('diagnostic errors omit URLs',()=>assert.equal(publicError(new Error('Failed https://journal.example/path?token=private')),'Failed [URL]'));
