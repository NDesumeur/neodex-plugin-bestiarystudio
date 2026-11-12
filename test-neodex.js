// lancé par le test « local » de NeoDex : l'onglet Monde (générateur de monde), sans moteur ni carte graphique
module.exports = async ({ pj, js, sleep }) => {
  const run = async c => { try { return await pj(c); } catch (e) { if (/pas de cadre/.test(e.message)) return js(c); throw e; } };
  const M = `document.querySelector('.nbs').__monde`;
  const eq = async (code, want, what) => { const got = await run(code); if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(what + ' : ' + JSON.stringify(got)); };
  await sleep(300);
  await run(`(document.querySelector('.nbs-tab[data-p=monde]').click(), 0)`); await sleep(800);
  await eq(`!!${M}.W8.world && document.querySelector('#wCv').width > 100`, true, 'carte dessinée');
  // la même graine redonne le même monde, une autre graine un autre
  const o = `{ graine: 'essai', taille: 's', terres: '0.42', factions: '4' }`;
  await eq(`${M}.makeWorld(${o}).villes.map(v => v.nom).join() === ${M}.makeWorld(${o}).villes.map(v => v.nom).join()`, true, 'graine stable');
  await eq(`${M}.makeWorld(${o}).villes.map(v => v.nom).join() === ${M}.makeWorld({ ...${o}, graine: 'autre' }).villes.map(v => v.nom).join()`, false, 'autre graine');
  await eq(`(() => { const w = ${M}.makeWorld(${o}); return [w.factions.length, w.villes.filter(v => v.capitale).length, w.villes.length >= 6, w.masses.some(m => m.genre === 'continent')]; })()`, [4, 4, true, true], 'factions et villes');
  await eq(`${M}.makeWorld({ graine: 'a', taille: 'm', terres: '0.3', factions: '5' }).masses.filter(m => m.genre === 'île').length > 0`, true, 'archipel');
  // la faune : une créature feu et eau vit au volcan et sur la côte, pas dans les neiges
  const lib = `[{ slug: 'x', name: 'X', type: 'feu et eau' }, { slug: 'y', name: 'Y', type: 'glace' }]`;
  await eq(`['volcan', 'cote', 'neige', 'jungle'].map(b => ${M}.habitants(${lib}, b).map(c => c.slug).join())`, ['x', 'x', 'y', ''], 'faune par biome');
  // l'export JSON et un clic sur la carte
  await eq(`(() => { const j = ${M}.wJson(); return [j.factions.length, j.cases.length === j.largeur * j.hauteur, j.villes.every(v => v.nom && v.biome)]; })()`, [5, true, true], 'export JSON');
  await run(`(() => { const c = document.querySelector('#wCv'), r = c.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    c.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, pointerId: 1, bubbles: true })); c.dispatchEvent(new PointerEvent('pointerup', { clientX: x, clientY: y, pointerId: 1, bubbles: true })); return 0; })()`);
  await sleep(200);
  await eq(`/Faune/.test(document.querySelector('#wInfo').textContent)`, true, 'infos du lieu');
  return 'monde : graine stable, factions, archipel, faune par biome, export JSON, clic sur la carte ok';
};
