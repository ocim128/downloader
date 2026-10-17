// M1 spike: compute Node-exact getToken() for the golden id set.
// getToken = ((Number(id)/1e15)*Math.PI).toString(36).replace(/(0+|\.)/g, '')
// Source of truth for tests/fixtures/golden_tokens.json (my Python port must match).
import { writeFileSync } from 'node:fs';

const IDS = [
  '20',
  '2058755555980681488',
  '2110047410852680127',
  '2057633662485540993',
  '2040511285998313827',
  '1553065817788407808',
  '2100595165371199589',
  '449660889793581056',
  '449198059382386688',
  '1683920951807971329',
  '2107273352608198907',
  '2106066240427147458',
  '1872514028259680687',
];

function getToken(id) {
  return ((Number(id) / 1e15) * Math.PI).toString(6 ** 2).replace(/(0+|\.)/g, '');
}

const goldens = IDS.map((id) => ({ id, token: getToken(id) }));
writeFileSync('tests/fixtures/golden_tokens.json', JSON.stringify(goldens, null, 2) + '\n');
console.log(JSON.stringify(goldens, null, 2));
