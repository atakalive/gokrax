#!/usr/bin/env node
/**
 * gateway-send.js — Gateway chat.send経由でエージェントにメッセージを送る
 * 
 * collectキュー（デフォルト）により、run中でもabortせずfollowup turnとして処理される。
 * stdinからメッセージを読み込むため、引数長制限(ARG_MAX 128KB)を回避できる。
 * chat.sendは改行を保持する。二重送信問題もない（openclaw agent CLI固有の問題）。
 * 
 * Usage: node gateway-send.js <sessionKey> [message]
 * (message省略時はstdinから読み込みます)
 */

const args = process.argv.slice(2);
const sessionKey = args[0];
const inlineMessage = args[1];

if (!sessionKey) {
  console.error('Usage: node gateway-send.js <sessionKey> [message]');
  process.exit(1);
}

async function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
  });
}

async function main() {
  let message = inlineMessage;
  if (!message) {
    message = await readStdin();
  }
  if (!message) {
    console.error('No message provided');
    process.exit(1);
  }

  const fs = await import('fs');
  const crypto = await import('crypto');
  const distDir = '/usr/lib/node_modules/openclaw/dist/';

  const callFile = fs.readdirSync(distDir)
    .filter(f => f.startsWith('call-') && f.endsWith('.js')).sort().pop();
  const { n: callGateway, c: ADMIN_SCOPE } = await import(`${distDir}${callFile}`);

  const mcCandidates = fs.readdirSync(distDir)
    .filter(f => f.startsWith('message-channel-') && f.endsWith('.js'));
  let mcFile;
  for (const c of mcCandidates) {
    const src = fs.readFileSync(distDir + c, 'utf8');
    if (src.includes('GATEWAY_CLIENT_NAMES as h')) { mcFile = c; break; }
  }
  if (!mcFile) throw new Error('No message-channel module found with expected exports');
  const { h: GATEWAY_CLIENT_NAMES, m: GATEWAY_CLIENT_MODES } = await import(`${distDir}${mcFile}`);

  try {
    const result = await callGateway({
      method: 'chat.send',
      params: {
        sessionKey,
        message,
        idempotencyKey: crypto.randomUUID(),
      },
      mode: GATEWAY_CLIENT_MODES.CLI,
      clientName: GATEWAY_CLIENT_NAMES.CLI,
      scopes: [ADMIN_SCOPE],
    });

    if (result?.error) {
      console.error('chat.send error:', JSON.stringify(result.error));
      process.exit(1);
    }
    process.exit(0);
  } catch (err) {
    console.error('Error:', err.message);
    process.exit(1);
  }
}

main();
