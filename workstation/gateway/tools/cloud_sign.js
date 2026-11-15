// Ed25519 request signatures. The private key stays on the owner's PC.
const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');
const [action, privatePath, publicOrRequest, signaturePath] = process.argv.slice(2);
if (action === 'init') {
  fs.mkdirSync(path.dirname(privatePath), {recursive:true});
  if (!fs.existsSync(privatePath)) {
    const pair = crypto.generateKeyPairSync('ed25519');
    fs.writeFileSync(privatePath, pair.privateKey.export({type:'pkcs8',format:'pem'}), {flag:'wx',mode:0o600});
  }
  const pub = crypto.createPublicKey(fs.readFileSync(privatePath)).export({type:'spki',format:'pem'});
  if (fs.existsSync(publicOrRequest) && !fs.readFileSync(publicOrRequest).equals(Buffer.from(pub))) throw new Error('existing public key differs');
  if (!fs.existsSync(publicOrRequest)) fs.writeFileSync(publicOrRequest, pub, {flag:'wx'});
  process.stdout.write(JSON.stringify({public_key_path:publicOrRequest,public_key_sha256:crypto.createHash('sha256').update(pub).digest('hex')}));
} else if (action === 'sign') {
  const data = Buffer.concat([Buffer.from('bench-eval-request-v1\0'), fs.readFileSync(publicOrRequest)]);
  const sig = crypto.sign(null, data, fs.readFileSync(privatePath));
  if (fs.existsSync(signaturePath)) {
    if (!fs.readFileSync(signaturePath).equals(sig)) throw new Error('existing signature differs');
  } else fs.writeFileSync(signaturePath, sig, {flag:'wx'});
  process.stdout.write(JSON.stringify({signed:true}));
} else throw new Error('unsupported signing action');
