/* live_notes — interface.
 *
 * Le navigateur ne sert plus qu'a dessiner et a enregistrer les metadonnees de
 * trace. L'export est integralement recalcule cote Python (voir renderer.py) :
 * on envoie du JSON, pas des images.
 */
(function () {
  'use strict';

  var BE = window.BrushEngine;

  /* ---------------------------------------------------------------- État */

  /* `background` : couleur du papier sur lequel on dessine, ou la sentinelle
   * CHECKER. Ce n'est pas une couleur transparente — on dessine toujours sur
   * une surface. Elle sert aussi de fond aux exports sans alpha (renderer.py). */
  var CHECKER = 'checker';
  var config = { width: 1920, height: 1080, fps: 25, alpha: true, background: '#ffffff' };
  var ready = false;

  /* Couches de dessin. `layers[0]` est au fond, la dernière au premier plan ;
   * `activeId` désigne la seule qui reçoive des traces.
   *
   * « Verrouillée » et « pas active » sont la même chose : il y a toujours
   * exactement une couche déverrouillée, et c'est l'active — un seul état à
   * tenir, au lieu d'un cadenas par couche à garder cohérent avec lui.
   *
   * Une couche n'a pas d'horloge à elle : le `t` de ses traces compte depuis
   * le point IN du média, comme partout ailleurs. C'est ce qui permet
   * d'ajouter un trait « à la douzième seconde » sur une prise déjà faite. */
  var layers = [];
  var activeId = 0;
  var layerSeq = 0;
  var currentBrush = null;
  var library = { presets: [], userPresets: [] };

  /* Réglages retouchés par l'utilisateur, mémorisés par pinceau : passer d'une
   * brosse à l'autre dans la palette conserve les modifications de chacune. */
  var EDITS_KEY = 'live_notes.brushEdits.v1';
  var brushEdits = readEdits();
  var saveEditsTimer = null;

  function readEdits() {
    try { return JSON.parse(localStorage.getItem(EDITS_KEY)) || {}; } catch (e) { return {}; }
  }

  function persistEdits() {
    if (saveEditsTimer) clearTimeout(saveEditsTimer);
    saveEditsTimer = setTimeout(function () {
      try { localStorage.setItem(EDITS_KEY, JSON.stringify(brushEdits)); } catch (e) { /* quota */ }
    }, 400);
  }

  /** Version effective d'un preset : ses retouches si elles existent. */
  function effective(preset) {
    return brushEdits[preset.id] || preset;
  }

  var drawing = false;
  var stroke = null;
  var stamper = null;
  /* Qui tient le trait en cours : un pincement abandonne celui d'un doigt,
   * jamais celui d'un stylet (voir « Pincement tactile »). */
  var strokePointer = 'mouse';

  var recording = false;
  var t0 = null;
  var recordStart = 0;
  var recordedDuration = 0;

  var previewing = false;
  /* Rejeu en cours (prévisualisation, ou couches verrouillées pendant le REC),
   * ou null. Voir « Rejeu ». */
  var replay = null;
  var recFrame = null;
  var underlayStart = null;   /* horloge du rejeu chez le suiveur (voir recElapsed) */
  var previewStart = 0;
  var previewFrame = null;
  var previewDuration = 0;

  var mediaType = null;
  var mediaNode = null;
  var mediaName = '';
  var mediaUrl = null;
  var mediaIsObjectUrl = false;   /* une URL objet se revoque, pas une URL serveur */
  var playbackListener = null;
  var stopWatchingOut = null;     /* désarme le guet de fin de plage du REC */
  var exportDirectory = '';

  /* Ajustement du média dans le cadre. `contain` fait entrer le média entier
   * (le fond du canevas apparaît sur les côtés) ; `crop` le recadre au format
   * du canevas, avec zoom (≥ 1) et position (0..1 sur chaque axe). */
  var mediaFit = { mode: 'contain', zoom: 1, posX: 0.5, posY: 0.5 };
  var mediaSize = { width: 0, height: 0 };

  /* Dernier cadrage publié par le poste, retenu sur la tablette seule. L'état
   * de session applique la configuration avant le média : sans cette mémoire,
   * le `releaseMedia()` de l'installation du rush effacerait le cadrage reçu
   * une fraction de seconde plus tôt. */
  var sessionFit = null;

  function cloneFit(fit) {
    return { mode: fit.mode, zoom: fit.zoom, posX: fit.posX, posY: fit.posY };
  }

  /* ------------------------------------------------------- Mode tablette */

  /* Le poste et la tablette executent ce meme fichier. Le role decide qui
   * dessine et qui regarde ; tout le reste (moteur de pinceaux, rejeu des
   * traces, transport) est rigoureusement commun — c'est ce qui garantit que
   * le trace vu sous le stylet est celui que le poste affiche, et celui que
   * `renderer.py` produira. */
  var REMOTE = window.LiveRemote;
  var isTablet = REMOTE.isTablet;

  var sessionMode = 'solo';       /* solo | pairing | live */
  var controller = 'pc';          /* qui tient le stylet : 'pc' ou 'tablet' */
  var viewing = false;            /* cet écran suit l'autre au lieu de dessiner */
  var incoming = {};              /* traces en cours de reception, par identifiant */
  var strokeSeq = 0;
  var sendingStroke = null;
  var pointBuffer = [];
  var lastTimeSync = 0;

  /** Sur la tablette, rien n'est pilotable localement tant que le poste n'a
   *  pas publie une configuration d'espace de travail. */
  var tabletReady = false;
  var tabletUiReady = false;
  var tabletBubbleTimers = {};
  var tabletTransportTimer = null;

  /* Vue du canevas : `zoom` 1 = ajusté à la fenêtre (fit), pan en pixels écran. */
  var view = { zoom: 1, panX: 0, panY: 0 };
  var fit = { width: 0, height: 0 };
  var ZOOM_MIN = 0.1;
  var ZOOM_MAX = 16;
  var panning = null;
  var shortcutState = {
    alt: false,
    ctrl: false,
    meta: false,
    space: false,
    opacity: false,
    eraser: false,
    spaceConsumed: false,
    pointer: null,
    hudTimer: null
  };

  var cursor = { x: 0, y: 0, visible: false, pending: false };

  var canvas = document.getElementById('drawing-canvas');
  var ctx = canvas.getContext('2d');
  var overlay = document.getElementById('cursor-overlay');
  var overlayCtx = overlay.getContext('2d');
  var baseCanvas = document.createElement('canvas');
  var baseCtx = baseCanvas.getContext('2d');
  var scratchCanvas = document.createElement('canvas');
  var scratchCtx = scratchCanvas.getContext('2d');

  var redrawPending = false;

  function $(id) { return document.getElementById(id); }
  function status(message) { $('status').textContent = message; }

  function setTabletDrawingState(active) {
    if (!isTablet) return;
    document.body.classList.toggle('tablet-drawing', active);
    document.body.classList.toggle('tablet-controls-docked', active);
  }

  function clearTabletBubbleTimer(id) {
    if (!tabletBubbleTimers[id]) return;
    clearTimeout(tabletBubbleTimers[id]);
    tabletBubbleTimers[id] = null;
  }

  function scheduleTabletBubbleIdle(id) {
    if (!isTablet) return;
    clearTabletBubbleTimer(id);
    tabletBubbleTimers[id] = setTimeout(function () {
      var node = $(id);
      if (node) node.classList.add('idle');
    }, 2000);
  }

  function wakeTabletBubble(id) {
    if (!isTablet) return;
    var node = $(id);
    if (!node) return;
    clearTabletBubbleTimer(id);
    node.classList.remove('idle');
    scheduleTabletBubbleIdle(id);
  }

  function bindTabletBubble(id) {
    if (!isTablet) return;
    var node = $(id);
    if (!node) return;
    ['pointerdown', 'pointermove', 'focusin'].forEach(function (name) {
      node.addEventListener(name, function () { wakeTabletBubble(id); });
    });
    ['pointerleave', 'focusout'].forEach(function (name) {
      node.addEventListener(name, function () { scheduleTabletBubbleIdle(id); });
    });
    scheduleTabletBubbleIdle(id);
  }

  function setTabletBrushOpen(open) {
    if (!isTablet || !tabletUiReady) return;
    $('tablet-brush-bubble').classList.toggle('is-open', open);
    $('tablet-brush-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) wakeTabletBubble('tablet-brush-bubble');
    else scheduleTabletBubbleIdle('tablet-brush-bubble');
  }

  function clearTabletTransportFade() {
    if (!tabletTransportTimer) return;
    clearTimeout(tabletTransportTimer);
    tabletTransportTimer = null;
  }

  function scheduleTabletTransportFade() {
    if (!isTablet || !$('transport').classList.contains('tablet-overlay-open')) return;
    clearTabletTransportFade();
    tabletTransportTimer = setTimeout(function () {
      if (drawing) return;
      $('transport').classList.add('tablet-subtle');
    }, 2000);
  }

  function wakeTabletTransport() {
    if (!isTablet) return;
    clearTabletTransportFade();
    $('transport').classList.remove('tablet-subtle');
    scheduleTabletTransportFade();
  }

  function setTabletTransportOpen(open) {
    if (!isTablet || !tabletUiReady) return;
    $('transport').classList.toggle('tablet-overlay-open', open);
    $('tablet-media-bubble').classList.toggle('is-open', open);
    $('tablet-media-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    $('tablet-media-toggle').textContent = open ? '✕' : '▶';
    if (open) {
      wakeTabletBubble('tablet-media-bubble');
      wakeTabletTransport();
    } else {
      clearTabletTransportFade();
      $('transport').classList.remove('tablet-subtle');
      scheduleTabletBubbleIdle('tablet-media-bubble');
    }
  }

  function initTabletInterface() {
    if (!isTablet || tabletUiReady) return;
    tabletUiReady = true;

    var libraryHost = $('tablet-brush-library-host');
    var brushTools = $('tablet-brush-tools');
    var controlHost = $('tablet-control-host');

    var libraryStack = $('brush-library').parentNode;
    var colorField = $('prop-color').parentNode;
    var bgField = $('bg-color').parentNode;
    libraryHost.appendChild(libraryStack);
    brushTools.appendChild(colorField);
    brushTools.appendChild(bgField);

    // Annuler et rétablir suivent le stylet : c'est la tablette qui dessine,
    // c'est elle qui doit pouvoir revenir en arrière — et elle n'a pas de
    // Ctrl + Z sous la main.
    ['btn-undo', 'btn-redo', 'btn-clear', 'btn-rec', 'btn-stop', 'btn-preview']
      .forEach(function (id) {
        controlHost.appendChild($(id));
      });

    $('transport').classList.remove('collapsed');
    $('tablet-brush-toggle').addEventListener('click', function (event) {
      event.stopPropagation();
      setTabletBrushOpen(!$('tablet-brush-bubble').classList.contains('is-open'));
    });
    $('tablet-brush-settings').addEventListener('click', function () {
      setTabletBrushOpen(false);
      setPanelCollapsed(false);
    });
    $('tablet-media-toggle').addEventListener('click', function () {
      setTabletTransportOpen(!$('transport').classList.contains('tablet-overlay-open'));
    });
    $('tablet-control-handle').addEventListener('click', function () {
      if (drawing) return;
      document.body.classList.toggle('tablet-controls-docked');
      wakeTabletBubble('tablet-control-bubble');
    });
    $('tablet-return').addEventListener('click', requestControlSwap);
    initTabletFullscreen();

    document.addEventListener('pointerdown', function (event) {
      if (!$('tablet-brush-bubble').contains(event.target) && !$('panel').contains(event.target)) {
        setTabletBrushOpen(false);
      }
    }, true);

    ['pointerdown', 'pointermove', 'focusin'].forEach(function (name) {
      $('transport').addEventListener(name, wakeTabletTransport);
    });
    ['pointerleave', 'focusout'].forEach(function (name) {
      $('transport').addEventListener(name, scheduleTabletTransportFade);
    });

    bindTabletBubble('tablet-brush-bubble');
    bindTabletBubble('tablet-control-bubble');
    bindTabletBubble('tablet-media-bubble');
    bindTabletBubble('tablet-return');
    bindTabletBubble('tablet-fullscreen');
    setTabletBrushOpen(false);
    setTabletTransportOpen(false);
    setTabletDrawingState(false);
  }

  /* Le stylet change de main depuis la tablette.
   *
   * Les routes d'appairage sont réservées au poste (`@pc_only` dans app.py) :
   * appelée depuis la tablette, `sessionAction` ne récoltait qu'un 403 — le
   * bouton ne pouvait pas marcher, même une fois les clics arrivés jusqu'à
   * lui. La tablette *demande* donc, et le poste — seul habilité — exécute.
   *
   * `REMOTE.send` directement, et non `emit` : celui-ci ne laisse passer que
   * le maître, or c'est précisément quand la tablette ne l'est pas qu'elle a
   * besoin de réclamer le stylet. */
  function requestControlSwap() {
    if (!isTablet) return;
    var want = controller === 'tablet' ? 'pc' : 'tablet';
    REMOTE.send({ t: 'control', want: want });
    status(want === 'pc' ? 'Main rendue au poste…' : 'Demande du stylet…');
  }

  function syncTabletControlButton() {
    var button = $('tablet-return');
    if (!button || !isTablet) return;
    var hasStylus = controller === 'tablet';
    button.textContent = hasStylus ? 'PC' : 'MAIN';
    button.setAttribute('aria-label',
      hasStylus ? 'Rendre la main au poste' : 'Prendre la main sur le poste');
  }

  /* ------------------------------------------------- Plein écran tablette */

  /* Gagner la hauteur de la barre d'adresse et des onglets de Safari change
   * beaucoup sur une tablette tenue à la main : c'est autant de canevas.
   * Une page web ne peut l'obtenir que par l'API plein écran, et seulement
   * en réponse à un geste — d'où un bouton et pas un appel au chargement.
   * Safari sur iPad ne connaît que les noms préfixés `webkit`. */

  function fullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function canGoFullscreen() {
    var root = document.documentElement;
    return !!(root.requestFullscreen || root.webkitRequestFullscreen);
  }

  function toggleFullscreen() {
    var root = document.documentElement;
    if (fullscreenElement()) {
      if (document.exitFullscreen) document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
      return;
    }
    // La promesse peut être rejetée (geste jugé insuffisant, réglage système) :
    // la rattraper évite une exception non gérée, et l'état du bouton se remet
    // de toute façon depuis l'évènement `fullscreenchange`.
    var request = root.requestFullscreen || root.webkitRequestFullscreen;
    var result = request.call(root);
    if (result && result.catch) {
      result.catch(function () { status('Le plein écran a été refusé par le navigateur.'); });
    }
  }

  function syncFullscreenButton() {
    var button = $('tablet-fullscreen');
    if (!button) return;
    var on = !!fullscreenElement();
    button.textContent = on ? '⤡' : '⤢';
    button.setAttribute('aria-label', on ? 'Quitter le plein écran' : 'Passer en plein écran');
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  function initTabletFullscreen() {
    var button = $('tablet-fullscreen');
    if (!button) return;
    // Déjà sans barre d'adresse (ajoutée à l'écran d'accueil), ou plateforme
    // qui ne sait pas le faire : un bouton sans effet vaut moins que pas de
    // bouton du tout.
    var standalone = window.navigator.standalone === true
      || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
    if (standalone || !canGoFullscreen()) return;

    button.style.display = '';
    button.addEventListener('click', toggleFullscreen);
    ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (name) {
      document.addEventListener(name, syncFullscreenButton);
    });
    syncFullscreenButton();
  }

  /* -------------------------------------------------- Paramètres pinceau */

  var SHAPES = [
    { value: 'round', label: 'Rond' },
    { value: 'rect', label: 'Biseau / rectangle' },
    { value: 'speckle', label: 'Taches' },
    { value: 'grain', label: 'Grain' },
    { value: 'texture', label: 'Texture importée' }
  ];

  /* `curve: 'log'` : le curseur est logarithmique. Indispensable dès qu'une
   * plage couvre 1 → 1000 px, sinon les tailles courantes (5-30 px) tiennent
   * dans les trois premiers pixels du slider. La valeur affichée, elle, reste
   * toujours la vraie valeur en pixels. */
  var PARAMS = [
    { section: 'Forme du pinceau' },
    { key: 'shape', label: 'Forme', type: 'select', options: SHAPES },
    { key: 'size', label: 'Taille', min: 1, max: 1000, step: 1, unit: 'px', curve: 'log' },
    { key: 'opacity', label: 'Opacité', min: 0, max: 1, step: 0.01, percent: true },
    { key: 'flow', label: 'Flux (dépôt par empreinte)', min: 0.02, max: 1, step: 0.01, percent: true },
    { key: 'hardness', label: 'Dureté', min: 0, max: 1, step: 0.01, percent: true },
    { key: 'spacing', label: 'Espacement', min: 0.01, max: 1, step: 0.01, percent: true },
    { key: 'aspect', label: 'Aplatissement', min: 0.02, max: 1, step: 0.01, percent: true, shapes: ['rect'] },
    { key: 'angle', label: 'Angle', min: -180, max: 180, step: 1, unit: '°' },

    { section: 'Variations aléatoires' },
    { key: 'jitter.position', label: 'Dispersion', min: 0, max: 1, step: 0.01, percent: true },
    { key: 'jitter.size', label: 'Taille', min: 0, max: 1, step: 0.01, percent: true },
    { key: 'jitter.angle', label: 'Rotation', min: 0, max: 180, step: 1, unit: '°' },
    { key: 'jitter.opacity', label: 'Opacité', min: 0, max: 1, step: 0.01, percent: true },

    { section: 'Réponse à la pression (stylet)' },
    { key: 'pressure.size', label: 'Pression → taille', min: 0, max: 1, step: 0.01, percent: true },
    { key: 'pressure.opacity', label: 'Pression → opacité', min: 0, max: 1, step: 0.01, percent: true }
  ];

  var PARAM_BY_KEY = {};
  PARAMS.forEach(function (param) {
    if (param.key) PARAM_BY_KEY[param.key] = param;
  });

  var widgets = [];              /* lignes de paramètre construites, pour syncPanel */
  var COLLAPSE_KEY = 'live_notes.collapsed.v1';

  function getPath(object, path) {
    return path.split('.').reduce(function (acc, key) { return acc == null ? acc : acc[key]; }, object);
  }

  function setPath(object, path, value) {
    var keys = path.split('.');
    var target = object;
    for (var i = 0; i < keys.length - 1; i++) {
      if (!target[keys[i]]) target[keys[i]] = {};
      target = target[keys[i]];
    }
    target[keys[keys.length - 1]] = value;
  }

  function clamp(value, lo, hi) { return value < lo ? lo : (value > hi ? hi : value); }

  function decimals(step) {
    var text = String(step);
    var dot = text.indexOf('.');
    return dot < 0 ? 0 : text.length - dot - 1;
  }

  function roundTo(value, step) { return parseFloat(value.toFixed(decimals(step))); }

  /* Conversions entre la valeur réelle, la position du curseur et la valeur
   * affichée à l'utilisateur (les pourcentages se saisissent en 0-100). */

  function toSlider(param, value) {
    if (param.curve !== 'log') return value;
    return 1000 * Math.log(value / param.min) / Math.log(param.max / param.min);
  }

  function fromSlider(param, position) {
    var raw = parseFloat(position);
    if (param.curve !== 'log') return raw;
    return roundTo(param.min * Math.pow(param.max / param.min, raw / 1000), param.step);
  }

  function toDisplay(param, value) {
    return param.percent ? Math.round(value * 100) : roundTo(value, param.step);
  }

  function fromDisplay(param, shown) { return param.percent ? shown / 100 : shown; }

  /* La gomme de la bibliothèque, avec les réglages que l'utilisateur.ice lui a
   * donnés. `E` doit sortir *cet* outil-là, le temps qu'on le maintient — pas
   * basculer le pinceau courant en mode gomme, ce qui gommait à la taille et à
   * la dureté du feutre en cours et ne ressemblait à rien de connu. */
  function eraserPreset() {
    var found = null;
    library.presets.concat(library.userPresets).forEach(function (preset) {
      if (!found && BE.normalize(effective(preset)).eraser) found = preset;
    });
    return found;
  }

  function shortcutBrush() {
    if (!currentBrush) return null;
    var brush = BE.normalize(currentBrush);
    if (!shortcutState.eraser || isTablet) return brush;

    var preset = eraserPreset();
    if (preset) return BE.normalize(effective(preset));
    // Aucune gomme dans la bibliothèque : plutôt que de ne rien gommer du
    // tout, on retombe sur l'ancien comportement.
    brush = JSON.parse(JSON.stringify(brush));
    brush.eraser = true;
    return brush;
  }

  function formatShortcutValue(param, value) {
    if (param.percent) return Math.round(value * 100) + ' %';
    return roundTo(value, param.step) + (param.unit ? ' ' + param.unit : '');
  }

  function showShortcutHud(message, sticky) {
    var hud = $('shortcut-hud');
    if (!hud) return;
    if (shortcutState.hudTimer) {
      clearTimeout(shortcutState.hudTimer);
      shortcutState.hudTimer = null;
    }
    hud.textContent = message;
    hud.classList.add('show');
    if (sticky) return;
    shortcutState.hudTimer = setTimeout(function () {
      hud.classList.remove('show');
      shortcutState.hudTimer = null;
    }, 700);
  }

  function hideShortcutHud() {
    if (shortcutState.hudTimer) {
      clearTimeout(shortcutState.hudTimer);
      shortcutState.hudTimer = null;
    }
    $('shortcut-hud').classList.remove('show');
  }

  /* ------------------------------------------------- Sections repliables */

  function readCollapsed() {
    try { return JSON.parse(localStorage.getItem(COLLAPSE_KEY)) || {}; } catch (e) { return {}; }
  }

  function rememberCollapsed(title, collapsed) {
    var state = readCollapsed();
    state[title] = collapsed;
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(state)); } catch (e) { /* quota */ }
  }

  function makeSection(title) {
    var section = document.createElement('div');
    section.className = 'section';
    if (readCollapsed()[title]) section.classList.add('collapsed');

    var head = document.createElement('button');
    head.className = 'section-head';
    var caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = '▼';
    head.appendChild(caret);
    head.appendChild(document.createTextNode(title));
    head.addEventListener('click', function () {
      section.classList.toggle('collapsed');
      rememberCollapsed(title, section.classList.contains('collapsed'));
    });

    var body = document.createElement('div');
    body.className = 'section-body';

    section.appendChild(head);
    section.appendChild(body);
    section._body = body;
    return section;
  }

  /* ------------------------------------------------- Ligne de paramètre */

  function makeStepButton(label, title) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'param-step';
    button.textContent = label;
    button.title = title;
    return button;
  }

  function makeParamRow(param) {
    var wrapper = document.createElement('div');
    wrapper.className = 'param';
    wrapper.dataset.key = param.key;
    if (param.shapes) wrapper.dataset.shapes = param.shapes.join(',');

    var head = document.createElement('div');
    head.className = 'param-head';
    var name = document.createElement('span');
    name.textContent = param.label;
    head.appendChild(name);
    wrapper.appendChild(head);

    if (param.type === 'select') {
      var select = document.createElement('select');
      param.options.forEach(function (option) {
        var node = document.createElement('option');
        node.value = option.value;
        node.textContent = option.label;
        select.appendChild(node);
      });
      select.addEventListener('change', function () {
        currentBrush.shape = select.value;
        onBrushChanged();
      });
      wrapper.appendChild(select);
      wrapper._sync = function (brush) { select.value = getPath(brush, param.key); };
      return wrapper;
    }

    var row = document.createElement('div');
    row.className = 'param-row';

    var minus = makeStepButton('−', 'Diminuer d’un cran');
    var range = document.createElement('input');
    range.type = 'range';
    if (param.curve === 'log') {
      range.min = 0; range.max = 1000; range.step = 1;
    } else {
      range.min = param.min; range.max = param.max; range.step = param.step;
    }
    var plus = makeStepButton('+', 'Augmenter d’un cran');

    // Saisie directe : taper 500 est plus rapide que viser au curseur.
    var number = document.createElement('input');
    number.type = 'number';
    number.className = 'param-value';
    number.min = toDisplay(param, param.min);
    number.max = toDisplay(param, param.max);
    number.step = param.percent ? 1 : param.step;

    var unit = document.createElement('span');
    unit.className = 'param-unit';
    unit.textContent = param.percent ? '%' : (param.unit || '');

    row.appendChild(minus);
    row.appendChild(range);
    row.appendChild(plus);
    row.appendChild(number);
    row.appendChild(unit);
    wrapper.appendChild(row);

    function write(value) {
      range.value = toSlider(param, value);
      if (document.activeElement !== number) number.value = toDisplay(param, value);
    }

    function apply(value) {
      if (!isFinite(value)) return;
      setPath(currentBrush, param.key, clamp(value, param.min, param.max));
      onBrushChanged();
    }

    function nudge(direction) {
      var current = getPath(BE.normalize(currentBrush), param.key);
      apply(roundTo(current + direction * param.step, param.step));
    }

    range.addEventListener('input', function () { apply(fromSlider(param, range.value)); });
    number.addEventListener('input', function () { apply(fromDisplay(param, parseFloat(number.value))); });
    number.addEventListener('blur', function () { write(getPath(BE.normalize(currentBrush), param.key)); });
    minus.addEventListener('click', function () { nudge(-1); });
    plus.addEventListener('click', function () { nudge(1); });

    wrapper._sync = function (brush) { write(getPath(brush, param.key)); };
    return wrapper;
  }

  /* Deux onglets dans le panneau de droite : le pinceau se règle, les couches
   * se rangent. Le panneau reste repliable comme avant — l'onglet replié
   * affiche le nom de celui qui est ouvert. */
  var panelTab = 'brush';

  function setPanelTab(name) {
    if (isTablet) name = 'brush';
    panelTab = name;
    var onLayers = name === 'layers';
    $('tab-brush').setAttribute('aria-selected', onLayers ? 'false' : 'true');
    $('tab-layers').setAttribute('aria-selected', onLayers ? 'true' : 'false');
    $('pane-brush').style.display = onLayers ? 'none' : '';
    $('pane-layers').style.display = onLayers ? '' : 'none';
    $('panel-tab-label').textContent = onLayers ? 'Couches' : 'Pinceau';
    if (onLayers) {
      $('panel-title').textContent = 'Couches';
      $('panel-hint').textContent = layers.length > 1
        ? 'Une seule couche reçoit les traces : celle qui est active.'
        : 'Verrouille la couche courante et ajoute par-dessus.';
      syncLayerPanel();
    } else {
      syncPanel();
    }
  }

  $('tab-brush').addEventListener('click', function () { setPanelTab('brush'); });
  $('tab-layers').addEventListener('click', function () { setPanelTab('layers'); });

  /* Les icônes de la liste sont dessinées, pas écrites : un emoji arrive avec
   * la police du système, sa propre couleur et sa propre chasse — au milieu
   * d'une interface entièrement en SVG monochrome, il détonne et change
   * d'allure d'un poste à l'autre. */
  var LAYER_ICONS = {
    eye: '<path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/>'
      + '<circle cx="12" cy="12" r="2.6"/>',
    eyeOff: '<path d="M4 5l16 14"/>'
      + '<path d="M9.3 7A9.7 9.7 0 0 1 12 6.6c6 0 9.5 5.4 9.5 5.4a17 17 0 0 1-3.2 3.7"/>'
      + '<path d="M6.4 8.6A16.6 16.6 0 0 0 2.5 12S6 17.4 12 17.4a9.6 9.6 0 0 0 3.3-.6"/>',
    lock: '<rect x="5.5" y="10.5" width="13" height="9.5" rx="1.6"/>'
      + '<path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    trash: '<path d="M5 7h14"/><path d="M9.5 7V5.2h5V7"/><path d="M7 7l.9 12.3h8.2L17 7"/>'
  };

  function layerIcon(name) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = LAYER_ICONS[name];
    return svg;
  }

  /** Dessine la liste des couches. Le premier plan en haut, comme partout.
   *
   *  Rien n'est reconstruit quand l'onglet est fermé : chaque fin de trace
   *  appelle cette fonction, et rebâtir une liste que personne ne regarde
   *  coûterait du temps au stylet qui, lui, dessine. */
  function syncLayerPanel() {
    var host = $('layer-list');
    if (!host) return;
    $('tab-layers-count').textContent = String(layers.length);
    // L'onglet est fermé : la liste sera redessinée à son ouverture.
    if (panelTab !== 'layers') return;
    host.textContent = '';

    // Premier plan en haut : on lit une pile du dessus.
    layers.slice().reverse().forEach(function (layer) {
      var row = document.createElement('div');
      row.className = 'layer-row'
        + (layer.id === activeId ? ' is-active' : '')
        + (layer.visible ? '' : ' is-hidden');

      var eye = document.createElement('button');
      eye.type = 'button';
      eye.className = 'btn-icon';
      eye.appendChild(layerIcon(layer.visible ? 'eye' : 'eyeOff'));
      eye.title = (layer.visible ? 'Masquer ' : 'Afficher ') + layer.name
        + ' — une couche masquée ne s’exporte pas';
      eye.setAttribute('aria-label', eye.title);
      eye.addEventListener('click', function () { toggleLayer(layer.id); });
      row.appendChild(eye);

      var pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'layer-pick';
      pick.setAttribute('aria-label', 'Rendre ' + layer.name + ' active');
      var name = document.createElement('span');
      name.className = 'layer-name';
      name.textContent = layer.name;
      var meta = document.createElement('span');
      meta.className = 'layer-meta';
      meta.textContent = layer.strokes.length + ' tracé(s)'
        + (layer.id === activeId ? ' · active' : '')
        + (layer.durationMs ? ' · ' + (layer.durationMs / 1000).toFixed(1) + ' s' : '');
      pick.appendChild(name);
      pick.appendChild(meta);
      pick.addEventListener('click', function () { setActiveLayer(layer.id); });
      row.appendChild(pick);

      if (layer.id !== activeId) {
        var lock = layerIcon('lock');
        lock.setAttribute('class', 'layer-lock');
        lock.setAttribute('role', 'img');
        lock.setAttribute('aria-label', 'Verrouillée');
        lock.removeAttribute('aria-hidden');
        var why = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        why.textContent = 'Verrouillée — cliquer son nom pour y revenir';
        lock.appendChild(why);
        row.appendChild(lock);
      }

      var kill = document.createElement('button');
      kill.type = 'button';
      kill.className = 'btn-icon';
      kill.appendChild(layerIcon('trash'));
      kill.title = 'Supprimer ' + layer.name;
      kill.setAttribute('aria-label', kill.title);
      kill.addEventListener('click', function () { removeLayer(layer.id); });
      row.appendChild(kill);

      host.appendChild(row);
    });
  }

  /* Le geste central : verrouiller ce qu'on vient de faire, et poser une
   * couche vide par-dessus. Un seul bouton, parce que c'est un seul geste —
   * verrouiller sans ajouter ne laisserait nulle part où dessiner. */
  function addLayer() {
    if (isTablet || !ready) return;
    if (recording) { status('Terminer l’enregistrement avant d’ajouter une couche.'); return; }
    stopPreview();
    var layer = makeLayer();
    layers.push(layer);
    activeId = layer.id;
    bindActiveLayer();
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    // Une couche vide n'a pas d'enregistrement à elle : l'origine des temps se
    // repose au prochain REC.
    t0 = null;
    lastElapsed = 0;
    syncLayerPanel();
    syncHistoryButtons();
    requestRedraw();
    updateExportState();
    publishLayers();
    status(layer.name + ' ajoutée — les couches du dessous sont verrouillées.');
  }

  function toggleLayer(id) {
    var layer = layerById(id);
    if (!layer) return;
    // Masquer la couche qui reçoit les traces reviendrait à dessiner sans rien
    // voir arriver : le geste est refusé, et la ligne d'état dit quoi faire.
    if (layer.id === activeId && layer.visible) {
      status('Rendre une autre couche active avant de masquer « ' + layer.name + ' ».');
      return;
    }
    // Masquer une couche rejouée sous le stylet la ferait disparaître au
    // milieu de sa propre animation.
    if (recording) { status('Terminer l’enregistrement avant de masquer une couche.'); return; }
    layer.visible = !layer.visible;
    syncLayerPanel();
    requestRedraw();
    updateExportState();
    publishLayers();
    status(layer.name + (layer.visible ? ' affichée.' : ' masquée — elle ne s’exportera pas.'));
  }

  function removeLayer(id) {
    var layer = layerById(id);
    if (!layer || isTablet) return;
    if (recording) { status('Terminer l’enregistrement avant de supprimer une couche.'); return; }
    // Supprimer n'est pas annulable : l'historique porte sur les gestes de
    // dessin, pas sur la structure du projet. La confirmation est le garde-fou.
    if (layer.strokes.length && !window.confirm(
      'Supprimer « ' + layer.name + ' » et ses ' + layer.strokes.length + ' tracé(s) ?')) return;
    stopPreview();
    // Il y a toujours au moins une couche : la dernière se vide au lieu de
    // disparaître, sans quoi il n'y aurait plus où dessiner.
    if (layers.length === 1) {
      // Elle est forcément l'active : il n'y en a pas d'autre.
      clearActiveLayer();
    } else {
      layers = layers.filter(function (item) { return item !== layer; });
      if (activeId === id) {
        activeId = layers[layers.length - 1].id;
        bindActiveLayer();
      }
    }
    syncLayerPanel();
    syncHistoryButtons();
    requestRedraw();
    updateExportState();
    publishLayers();
    status(layer.name + ' supprimée.');
  }

  $('btn-layer-add').addEventListener('click', addLayer);
  $('btn-layer-new').addEventListener('click', function () {
    addLayer();
    setPanelTab('layers');
    setPanelCollapsed(false);
  });
  $('btn-undo').addEventListener('click', function () { undo(); });
  $('btn-redo').addEventListener('click', function () { redo(); });

  /* Le panneau se replie en onglet : le plan de travail récupère la largeur,
   * et le ResizeObserver réajuste l'image toute seule. */
  var PANEL_KEY = 'live_notes.panelCollapsed';

  function setPanelCollapsed(collapsed) {
    if (isTablet) {
      $('panel').classList.remove('collapsed');
      document.body.classList.toggle('tablet-panel-open', !collapsed);
      $('panel-toggle').textContent = '✕';
      $('panel-toggle').title = collapsed
        ? 'Ouvrir les réglages du pinceau'
        : 'Fermer les réglages du pinceau';
      return;
    }
    $('panel').classList.toggle('collapsed', collapsed);
    $('panel-toggle').textContent = collapsed ? '‹' : '›';
    $('panel-toggle').title = collapsed ? 'Déplier le panneau' : 'Replier le panneau';
    try { localStorage.setItem(PANEL_KEY, collapsed ? '1' : '0'); } catch (e) { /* quota */ }
  }

  $('panel-toggle').addEventListener('click', function () {
    setPanelCollapsed(!$('panel').classList.contains('collapsed'));
  });

  if (isTablet) setPanelCollapsed(true);
  else {
    try { setPanelCollapsed(localStorage.getItem(PANEL_KEY) === '1'); } catch (e) { /* ignore */ }
  }
  setPanelTab('brush');

  function buildPanel() {
    var host = $('panel-params');
    host.innerHTML = '';
    widgets = [];

    var body = null;
    PARAMS.forEach(function (param) {
      if (param.section) {
        var section = makeSection(param.section);
        host.appendChild(section);
        body = section._body;
        return;
      }
      var row = makeParamRow(param);
      widgets.push({ param: param, node: row });
      body.appendChild(row);
    });
  }

  function syncPanel() {
    var brush = shortcutBrush();
    if (!brush) return;
    // L'en-tête appartient à l'onglet ouvert : écrire le nom du pinceau
    // pendant qu'on range ses couches le remplacerait sous les doigts.
    if (panelTab === 'brush') {
      $('panel-title').textContent = currentBrush.name || 'Pinceau';
      $('panel-hint').textContent = brush.eraser
        ? 'Gomme — la couleur est ignorée.'
        : 'Les réglages s’appliquent au pinceau sélectionné.';
    }

    widgets.forEach(function (widget) {
      if (widget.node.dataset.shapes) {
        widget.node.style.display =
          widget.node.dataset.shapes.split(',').indexOf(brush.shape) >= 0 ? '' : 'none';
      }
      widget.node._sync(brush);
    });

    $('prop-color').value = brush.color;
    $('prop-color').disabled = brush.eraser;

    // Les pinceaux livrés avec l'outil ne se suppriment pas : ils viennent de
    // `brushes.json`, la bibliothèque de référence.
    var deletable = currentBrush && isUserPreset(currentBrush.id);
    $('btn-delete-brush').disabled = !deletable;
    $('btn-delete-brush').title = deletable
      ? 'Supprimer définitivement ce pinceau'
      : 'Les pinceaux de base ne peuvent pas être supprimés — enregistrez un preset pour créer le vôtre.';

    drawCursorPreview();
  }

  function onBrushChanged() {
    if (currentBrush && currentBrush.id) {
      brushEdits[currentBrush.id] = JSON.parse(JSON.stringify(currentBrush));
      persistEdits();
    }
    syncPanel();
    refreshActiveCard();
  }

  /* ------------------------------------------------ Bibliothèque de brosses */

  function loadLibrary() {
    return fetch('/api/brushes')
      .then(function (response) { return response.json(); })
      .then(function (data) {
        BE.setDefaults(data.defaults);
        library.presets = data.presets || [];
        library.userPresets = data.userPresets || [];
        renderLibrary();
        if (!currentBrush && library.presets.length) selectBrush(library.presets[0]);
      })
      .catch(function () {
        status('Bibliothèque de pinceaux indisponible — vérifiez que le serveur Python tourne.');
      });
  }

  function renderLibrary() {
    var host = $('brush-library');
    host.innerHTML = '';
    library.presets.forEach(function (preset) { host.appendChild(makeCard(preset, false)); });
    library.userPresets.forEach(function (preset) { host.appendChild(makeCard(preset, true)); });
    refreshActiveCard();
    syncLibraryScroll();
  }

  /* Défilement de la bibliothèque : un curseur maison, comme pour l'image.
   * Il reste toujours en place — seule sa visibilité change — pour que la
   * hauteur de la barre d'outils ne bouge jamais. */
  function syncLibraryScroll() {
    var host = $('brush-library');
    var slider = $('brush-scroll');
    var max = host.scrollWidth - host.clientWidth;
    slider.classList.toggle('idle', max < 1);
    if (max < 1) return;
    slider.max = max;
    if (document.activeElement !== slider) slider.value = host.scrollLeft;
  }

  $('brush-scroll').addEventListener('input', function () {
    $('brush-library').scrollLeft = parseFloat($('brush-scroll').value);
  });

  $('brush-library').addEventListener('scroll', syncLibraryScroll);
  // La carte active s'élargit en 160 ms : on resynchronise en fin d'animation.
  $('brush-library').addEventListener('transitionend', syncLibraryScroll);
  window.addEventListener('resize', syncLibraryScroll);

  function makeCard(preset, removable) {
    var card = document.createElement('div');
    card.className = 'brush-card';
    card.dataset.id = preset.id;

    var thumb = document.createElement('canvas');
    thumb.width = 176;
    thumb.height = 56;
    card.appendChild(thumb);

    var label = document.createElement('span');
    label.textContent = preset.name || preset.id;
    card.appendChild(label);

    BE.drawPreview(thumb, effective(preset), { maxSize: 26 });
    card.title = describe(effective(preset));
    card.addEventListener('click', function () { selectBrush(preset); });

    if (removable) {
      var remove = document.createElement('button');
      remove.className = 'remove';
      remove.title = 'Supprimer ce pinceau';
      remove.setAttribute('aria-label', 'Supprimer ce pinceau');
      remove.innerHTML = '<svg width="10" height="10" viewBox="0 0 16 16" fill="none"'
        + ' stroke="currentColor" stroke-width="1.7" stroke-linecap="round"'
        + ' stroke-linejoin="round" aria-hidden="true">'
        + '<path d="M2.5 4h11M6.3 4V2.4h3.4V4M4.2 4l.7 9.6h6.2L11.8 4"/></svg>';
      remove.addEventListener('click', function (event) {
        event.stopPropagation();
        confirmDelete(preset);
      });
      card.appendChild(remove);
    }
    return card;
  }

  function isUserPreset(id) {
    return library.userPresets.some(function (preset) { return preset.id === id; });
  }

  function confirmDelete(preset) {
    var name = preset.name || preset.id;
    if (!window.confirm('Supprimer définitivement le pinceau « ' + name + ' » ?\n\n'
      + 'Cette action est irréversible.')) return;
    deletePreset(preset.id, name);
  }

  function selectBrush(preset) {
    currentBrush = JSON.parse(JSON.stringify(effective(preset)));
    currentBrush.id = preset.id;
    currentBrush.name = preset.name;
    onBrushChanged();
    if (isTablet) setTabletBrushOpen(false);
    // Le poste affiche le pinceau tenu par la tablette : le réglage lui-même
    // voyage déjà avec chaque tracé, seul le nom manque à l'écran.
    emit({ t: 'brush', name: currentBrush.name });
  }

  /** Infobulle de survol : les réglages réels de la brosse, sans la sélectionner. */
  function describe(preset) {
    var brush = BE.normalize(preset);
    return (preset.name || preset.id) + '\n'
      + Math.round(brush.size) + ' px · opacité ' + Math.round(brush.opacity * 100) + ' %'
      + (brush.eraser ? ' · gomme' : ' · dureté ' + Math.round(brush.hardness * 100) + ' %');
  }

  function refreshActiveCard() {
    if (!currentBrush) return;
    Array.prototype.forEach.call($('brush-library').children, function (card) {
      var active = card.dataset.id === currentBrush.id;
      card.classList.toggle('active', active);
      if (active) {
        BE.drawPreview(card.firstChild, currentBrush, { maxSize: 26 });
        card.title = describe(currentBrush);
      }
    });
  }

  function promptUser(title, defaultValue) {
    // `window.prompt` ne s'affiche pas dans la fenetre macOS (WKWebView ne
    // l'implémente pas : il renvoie falsy et la sauvegarde de preset echouait
    // sans un mot). Une modale interne rend le nom du preset possible partout.
    return new Promise(function (resolve) {
      $('prompt-title').textContent = title;
      $('prompt-input').value = defaultValue || '';
      var done = function (value) {
        $('prompt-screen').style.display = 'none';
        $('prompt-ok').removeEventListener('click', onOk);
        $('prompt-cancel').removeEventListener('click', onCancel);
        $('prompt-input').removeEventListener('keydown', onKey);
        resolve(value);
      };
      var onOk = function () { done($('prompt-input').value); };
      var onCancel = function () { done(null); };
      var onKey = function (event) {
        if (event.key === 'Enter') onOk();
        else if (event.key === 'Escape') onCancel();
      };
      $('prompt-ok').addEventListener('click', onOk);
      $('prompt-cancel').addEventListener('click', onCancel);
      $('prompt-input').addEventListener('keydown', onKey);
      $('prompt-screen').style.display = 'flex';
      $('prompt-input').focus();
      $('prompt-input').select();
    });
  }

  function savePreset() {
    var brush = currentBrush;
    if (!brush) return;
    promptUser('Nom du preset :', (brush.name || 'Pinceau') + ' perso')
      .then(function (name) {
        if (!name) return;
        var preset = JSON.parse(JSON.stringify(BE.normalize(brush)));
        preset.name = name;
        delete preset.id;

        fetch('/api/presets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(preset)
        })
          .then(function (response) { return response.json(); })
          .then(function (data) {
            if (data.error) { status(data.error); return; }
            library.userPresets = data.userPresets;
            currentBrush = data.preset;
            renderLibrary();
            onBrushChanged();
            status('Preset « ' + name + ' » enregistré.');
          })
          .catch(function () { status('Impossible d’enregistrer le preset.'); });
      });
  }

  function deletePreset(id, name) {
    fetch('/api/presets/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        library.userPresets = data.userPresets;
        // Ses retouches n'ont plus d'objet, et le pinceau courant a disparu.
        delete brushEdits[id];
        persistEdits();
        if (currentBrush && currentBrush.id === id) {
          var fallback = library.presets[0] || library.userPresets[0];
          if (fallback) selectBrush(fallback);
        }
        renderLibrary();
        status('Pinceau « ' + (name || id) + ' » supprimé.');
      })
      .catch(function () { status('Suppression impossible — le serveur Python répond-il ?'); });
  }

  /* --------------------------------------------------------------- Dessin */

  function pointerPosition(event) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * (canvas.width / rect.width),
      y: (event.clientY - rect.top) * (canvas.height / rect.height)
    };
  }

  function pressureOf(event) {
    if (event.pointerType === 'mouse') return 1;
    return event.pressure > 0 ? event.pressure : 0.5;
  }

  function elapsed() {
    if (t0 === null) t0 = performance.now();
    var value = performance.now() - t0;
    // Le recalage sur l'horloge du rush (`syncClockToMedia`) peut déplacer
    // l'origine dans les deux sens ; les horodatages, eux, ne reculent jamais.
    // Le rejeu — à l'écran comme à l'export — parcourt les points dans l'ordre
    // où ils ont été posés, et un instant qui revient en arrière y ferait
    // apparaître un point avant celui qui l'a précédé.
    if (value < lastElapsed) value = lastElapsed;
    lastElapsed = value;
    return value;
  }

  var lastElapsed = 0;

  /* Recalage de l'horloge du tracé sur celle du rush.
   *
   * Les deux avancent du même pas tant que rien ne les dérange. Mais le
   * lecteur d'une tablette reçoit son rush par le réseau : il se met en pause
   * tout seul, le temps de remplir son tampon, sans que rien ne le signale.
   * L'horloge du système, elle, continue — et les points posés ensuite sont
   * horodatés en avance sur l'image qu'ils annotent. L'écart ne se rattrape
   * pas : il s'ajoute à chaque hoquet, et l'export hérite du total.
   *
   * On ramène donc l'origine sur le rush dès que l'écart dépasse une image et
   * demie. En dessous, c'est la granularité de `currentTime` que l'on
   * mesurerait, et corriger ne ferait qu'agiter les horodatages. */
  function syncClockToMedia(at) {
    if (t0 === null || !MediaTransport.isTimed()) return;
    var wanted = performance.now() - (at - inPoint()) * 1000;
    if (Math.abs(wanted - t0) < 1.5 * MediaTransport.frameStep() * 1000) return;
    t0 = wanted;
    recordStart = t0;
  }

  /* Origine des temps commune au tracé et au rush.
   *
   * `play()` est asynchrone : entre l'appel et la première image réellement
   * présentée s'écoulent le temps du positionnement et du décodage — quelques
   * dizaines de millisecondes sur un fichier local, bien davantage sur un
   * proxy servi par le réseau à une tablette, et un montant différent à chaque
   * prise. Démarrer l'horloge du tracé sur l'appel plutôt que sur cette
   * première image décalait donc tout l'enregistrement d'un délai variable :
   * le tracé ne retombait plus en face du rush, et l'export héritait du
   * décalage.
   *
   * `requestVideoFrameCallback` est le seul signal qui dise « cette image est
   * à l'écran ». À défaut, `playing` est le meilleur approchant. Le délai de
   * garde est un filet : un décodeur muet ne doit pas empêcher de démarrer.
   *
   * Savoir *quand* la première image est apparue ne suffit pourtant pas : il
   * faut savoir **laquelle**. Un fichier local repart pile sur le point IN,
   * mais un proxy servi par le réseau à une tablette rend souvent sa première
   * image une ou deux plus loin — le décodeur rattrape son retard en sautant
   * ce qu'il n'a pas eu le temps de présenter, et rien ne le dit. Compter le
   * tracé à partir de zéro décalait alors tout l'enregistrement d'autant :
   * c'est le décalage d'une ou deux images que l'on voyait sur tablette, et
   * jamais sur le poste.
   *
   * `onFirstFrame` reçoit donc ce retard, en millisecondes : de combien le
   * rush avait déjà dépassé son point IN au moment où on l'a vu. */
  function playFromInPoint(onFirstFrame) {
    if (!mediaNode || mediaType === 'image') { onFirstFrame(0); return; }

    var start = inPoint();
    var fired = false;
    function fire(lag) {
      if (fired) return;
      fired = true;
      onFirstFrame(Math.max(0, lag || 0));
    }

    /** Retard lu sur le lecteur, faute de mieux que `mediaTime`. */
    function lagFromClock() {
      return ((mediaNode.currentTime || start) - start) * 1000;
    }

    if (typeof mediaNode.requestVideoFrameCallback === 'function') {
      mediaNode.requestVideoFrameCallback(function (now, metadata) {
        // `mediaTime` est l'horodatage de l'image réellement présentée : le
        // seul qui dise où en est le rush à cet instant précis.
        fire(metadata && typeof metadata.mediaTime === 'number'
          ? (metadata.mediaTime - start) * 1000
          : lagFromClock());
      });
    } else {
      mediaNode.addEventListener('playing', function () { fire(lagFromClock()); }, { once: true });
    }
    setTimeout(function () { fire(lagFromClock()); }, 1500);

    seekToInPoint();
    var started = mediaNode.play();
    if (started && started.catch) started.catch(function () { fire(0); });
  }

  /** Positionne le rush sur son point IN sans le lancer.
   *
   *  Au *milieu* de la première image de la plage, jamais sur sa frontière : un
   *  décodeur peut retomber d'un côté comme de l'autre d'un instant qui sépare
   *  deux images, et la prise commençait alors une image trop tôt. */
  function seekToInPoint() {
    if (!mediaNode || mediaType === 'image') return;
    try {
      mediaNode.currentTime = MediaTransport.isTimed()
        ? MediaTransport.firstFrameTime() : inPoint();
    } catch (e) { /* pas encore seekable */ }
  }

  function desktopShortcutsEnabled() {
    return !isTablet && ready;
  }

  function stopPanning() {
    panning = null;
    $('media-container').classList.remove('panning');
  }

  function setShortcutPointer(mode, event, extra) {
    shortcutState.pointer = {
      mode: mode,
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      dx: 0            // déplacement cumulé, seule mesure valable sous verrou
    };
    if (extra) {
      for (var key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) shortcutState.pointer[key] = extra[key];
      }
    }
    canvas.setPointerCapture(event.pointerId);
  }

  function clearShortcutPointer(event) {
    if (!shortcutState.pointer) return;
    try { canvas.releasePointerCapture((event && event.pointerId) || shortcutState.pointer.id); } catch (e) { /* déjà relâché */ }
    shortcutState.pointer = null;
    hideShortcutHud();
  }

  /** Déplacement horizontal depuis le début du geste. */
  function shortcutDelta(event) {
    return event.clientX - shortcutState.pointer.x;
  }

  /** Un réglage de pinceau au glisser est-il en cours ? */
  function adjustingBrush() {
    return !!shortcutState.pointer && shortcutState.pointer.mode === 'brush';
  }

  function adjustedShortcutValue(param, startValue, deltaX) {
    if (param.curve === 'log') {
      var ratio = Math.log(param.max / param.min);
      var base = Math.log(Math.max(param.min, startValue) / param.min);
      return roundTo(clamp(param.min * Math.exp(base + deltaX * ratio / 320), param.min, param.max), param.step);
    }
    return roundTo(clamp(startValue + deltaX * (param.max - param.min) / 240, param.min, param.max), param.step);
  }

  function startBrushShortcut(event, key) {
    var param = PARAM_BY_KEY[key];
    if (!param || !currentBrush) return false;
    event.preventDefault();
    setShortcutPointer('brush', event, {
      key: key,
      label: param.label,
      // Lire sur l'objet que l'on va écrire : `shortcutBrush()` peut désigner
      // la gomme temporaire, dont les réglages ne sont pas ceux qu'on modifie.
      startValue: getPath(BE.normalize(currentBrush), key)
    });
    showShortcutHud(param.label + ' : ' + formatShortcutValue(param, shortcutState.pointer.startValue), true);
    return true;
  }

  function startZoomShortcut(event) {
    event.preventDefault();
    setShortcutPointer('zoom', event, {
      anchorX: event.clientX,
      anchorY: event.clientY,
      startZoom: view.zoom
    });
    showShortcutHud('Zoom : ' + Math.round(displayScale() * 100) + ' %', true);
  }

  function startSpacePan(event) {
    event.preventDefault();
    shortcutState.spaceConsumed = true;
    panning = { x: event.clientX, y: event.clientY };
    $('media-container').classList.add('panning');
  }

  /* ------------------------------ Couches verrouillées pendant le REC */

  /* Pendant qu'on enregistre la couche 2, la couche 1 se rejoue animée,
   * dessous, calée sur le média. C'est ce qui permet de poser un trait au bon
   * moment sur une prise déjà faite — sans ça, on dessinerait à l'aveugle
   * au-dessus d'une image figée. */

  function startUnderlay() {
    var others = layers.filter(function (layer) {
      return layer !== activeLayer() && layer.visible && layer.strokes.length;
    });
    // Le rejeu repart de zéro : leurs canevas doivent partir vides, sans quoi
    // l'état final resterait affiché par-dessus le rejeu naissant.
    others.forEach(function (layer) {
      layer.ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    });
    replay = others.length ? makeReplay(others) : null;
    requestRedraw();
  }

  function stopUnderlay() {
    if (recFrame) cancelAnimationFrame(recFrame);
    recFrame = null;
    underlayStart = null;
    if (!replay) return;
    replay = null;
    // Les couches rejouées sont peut-être arrêtées en plein milieu : on les
    // remet dans leur état final, celui qu'elles ont vraiment.
    layers.forEach(function (layer) {
      if (layer !== activeLayer()) rebuildLayer(layer);
    });
    requestRedraw();
  }

  /** L'instant courant du REC, sans jamais poser l'origine. `elapsed()` la
   *  pose quand elle manque, ce qui est juste pour un point tracé mais faux
   *  ici : l'origine doit rester la première image du rush, pas la première
   *  image d'animation qui passe. */
  function recElapsed() {
    if (t0 !== null) return Math.max(0, performance.now() - t0);
    // Le suiveur ne pose aucun point : il n'a pas d'origine des temps à lui,
    // seulement un rejeu à faire avancer. Lui laisser écrire `t0` reviendrait
    // à figer l'origine sur l'instant d'arrivée du message plutôt que sur la
    // première image du rush — et ce décalage-là suivrait ensuite tout ce
    // qu'il dessinerait s'il reprenait la main.
    return underlayStart === null ? 0 : Math.max(0, performance.now() - underlayStart);
  }

  function recTick() {
    if (!recording || !replay) { recFrame = null; return; }
    // Le stylet est prioritaire : on ne recompose la pile que quand les
    // couches du dessous ont vraiment quelque chose de nouveau à montrer.
    if (feedReplay(replay, recElapsed())) redraw();
    recFrame = requestAnimationFrame(recTick);
  }

  /* ------------------------------------------------- Gestes et historique */

  /* Deux gestes sont annulables, et deux seulement : poser une trace, et
   * effacer la toile pendant le REC. La profondeur, c'est la couche —
   * « Annuler » remonte jusqu'à la première trace de la couche active et
   * s'arrête là. Pas de limite en nombre : une trace ne pèse que ses points,
   * garder mille gestes coûte moins qu'une seule image de canevas. */

  function recordStroke(layer, item) {
    if (!layer) return;
    layer.strokes.push(item);
    layer.acts.push('s');
    // Un nouveau geste referme l'avenir qu'on venait d'annuler.
    layer.undone = [];
    syncLayerPanel();
    syncHistoryButtons();
  }

  /** Effacement daté, appliqué à une couche : elle repart à zéro à cet
   *  instant, les autres continuent. */
  function applyClear(layer, at) {
    if (!layer) return;
    layer.clears.push(at);
    layer.acts.push('c');
    layer.undone = [];
    layer.ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    syncLayerPanel();
    syncHistoryButtons();
    requestRedraw();
  }

  function undo(quiet) {
    var layer = activeLayer();
    if (!layer || !layer.acts.length) return false;
    var kind = layer.acts.pop();
    layer.undone.push(kind === 's'
      ? { k: 's', v: layer.strokes.pop() }
      : { k: 'c', v: layer.clears.pop() });
    afterHistory(layer);
    if (!quiet) {
      emit({ t: 'undo' });
      status(kind === 's'
        ? 'Trace annulée — ' + layer.acts.length + ' geste(s) annulable(s).'
        : 'Effacement annulé — ' + layer.acts.length + ' geste(s) annulable(s).');
    }
    return true;
  }

  function redo(quiet) {
    var layer = activeLayer();
    if (!layer || !layer.undone.length) return false;
    var step = layer.undone.pop();
    if (step.k === 's') layer.strokes.push(step.v); else layer.clears.push(step.v);
    layer.acts.push(step.k);
    afterHistory(layer);
    if (!quiet) {
      emit({ t: 'redo' });
      status('Geste rétabli — ' + layer.undone.length + ' à rétablir.');
    }
    return true;
  }

  /* Le canevas de la couche se reconstruit : une trace retirée ne laisse
   * aucune trace, et un effacement annulé fait réapparaître ce qu'il avait
   * masqué. Reconstruire ne coûte que le redessin des traces de cette
   * couche-là — jamais celles des autres, qui n'ont pas bougé. */
  function afterHistory(layer) {
    rebuildLayer(layer);
    syncLayerPanel();
    syncHistoryButtons();
    requestRedraw();
    updateExportState();
  }

  function syncHistoryButtons() {
    var layer = activeLayer();
    var busy = previewing || viewing;
    $('btn-undo').disabled = busy || !layer || !layer.acts.length;
    $('btn-redo').disabled = busy || !layer || !layer.undone.length;
  }

  /* ------------------------------------------------- Lecture d'un projet */

  var PROJECT_VERSION = 2;

  /** Les couches d'un fichier projet, ou null s'il n'en décrit aucune.
   *  Un projet v1 est une seule couche : c'est exactement ce qu'il était. */
  function projectLayers(data) {
    if (Array.isArray(data.layers)) {
      var ok = data.layers.every(function (raw) {
        return raw && Array.isArray(raw.strokes) && Array.isArray(raw.clears);
      });
      return ok ? data.layers : null;
    }
    if (Array.isArray(data.strokes) && Array.isArray(data.clears)) {
      return [{ name: 'Couche 1', visible: true, strokes: data.strokes,
        clears: data.clears, durationMs: Number(data.durationMs) || 0 }];
    }
    return null;
  }

  /** Remplace les couches courantes par celles d'un projet. Les canevas sont
   *  vides en sortie : c'est au chargeur de les reconstruire. */
  /** Une trace dont on peut faire quelque chose : le rejeu lit `points[0].t`
   *  sans détour, et un fichier tronqué en plein enregistrement ne doit pas
   *  faire tomber la prévisualisation entière. */
  function usableStroke(item) {
    return !!item && Array.isArray(item.points) && item.points.length > 0
      && typeof item.points[0].t === 'number';
  }

  function adoptLayers(raw, wantedActive) {
    layerSeq = 0;
    layers = raw.map(function (item) {
      var layer = makeLayer(item.name);
      layer.visible = item.visible !== false;
      layer.strokes = item.strokes.filter(usableStroke);
      layer.clears = item.clears;
      layer.durationMs = Number(item.durationMs) || 0;
      // L'historique ne traverse pas un enregistrement : on ne rejoue pas
      // l'histoire d'une session précédente.
      layer.acts = [];
      layer.undone = [];
      layer.sourceId = item.id;
      return layer;
    });
    if (!layers.length) layers = [makeLayer()];
    var wanted = layers.filter(function (layer) { return layer.sourceId === wantedActive; })[0];
    activeId = (wanted || layers[layers.length - 1]).id;
    bindActiveLayer();
    syncLayerPanel();
  }

  /** L'état des couches tel qu'il voyage vers l'autre écran. */
  function layerState() {
    return layers.map(function (layer) {
      return { id: layer.id, name: layer.name, visible: layer.visible };
    });
  }

  /** Publie la pile vers la tablette.
   *
   *  Pas par `emit` : celui-ci ne laisse passer que le détenteur du stylet, et
   *  le poste structure les couches précisément pendant que la tablette
   *  dessine. Les deux écrans se mettraient alors à ranger les mêmes tracés
   *  dans des couches différentes, et c'est la charge du poste qui part à
   *  l'export. C'est le poste qui fait autorité sur la pile, qu'il tienne le
   *  stylet ou non. */
  function publishLayers() {
    if (isTablet || sessionMode !== 'live' || !REMOTE.isOnline()) return;
    REMOTE.send({ t: 'layers', list: layerState(), activeId: activeId });
  }

  /* ------------------------------------------------------------ Couches */

  /* Une couche est exactement ce que le projet contenait déjà — des traces et
   * des effacements — mis dans une boîte nommée, plus le canevas où elle
   * s'accumule. Aucun champ nouveau sur une trace : le moteur de pinceaux
   * n'est pas touché, ni ici ni côté Python.
   *
   * `acts` est le journal d'insertion des gestes, dans l'ordre : il dit lequel
   * d'un tracé ou d'un effacement est arrivé en dernier, ce que la seule
   * lecture des deux tableaux ne permet pas de retrouver. C'est lui que
   * remonte « Annuler ». */

  function makeLayer(name) {
    var node = document.createElement('canvas');
    node.width = config.width;
    node.height = config.height;
    layerSeq += 1;
    return {
      id: layerSeq,
      name: name || ('Couche ' + layerSeq),
      visible: true,
      strokes: [],
      clears: [],
      durationMs: 0,
      acts: [],
      undone: [],
      canvas: node,
      ctx: node.getContext('2d')
    };
  }

  function layerById(id) {
    for (var i = 0; i < layers.length; i++) {
      if (layers[i].id === id) return layers[i];
    }
    return null;
  }

  function activeLayer() {
    return layerById(activeId) || layers[layers.length - 1] || null;
  }

  /** Nombre total de traces, toutes couches confondues. */
  function strokeCount() {
    return layers.reduce(function (n, layer) { return n + layer.strokes.length; }, 0);
  }

  /** Traces qui partiront réellement à l'export : une couche masquée n'y est
   *  pas, et c'est la même notion qu'à l'écran — pas deux réglages. */
  function visibleStrokeCount() {
    return layers.reduce(function (n, layer) {
      return n + (layer.visible ? layer.strokes.length : 0);
    }, 0);
  }

  /** Pointe `baseCanvas` / `baseCtx` sur la couche active : tout ce qui
   *  dessine en direct continue d'écrire là, sans savoir qu'il y a des
   *  couches. */
  function bindActiveLayer() {
    var layer = activeLayer();
    if (!layer) return;
    baseCanvas = layer.canvas;
    baseCtx = layer.ctx;
  }

  function setActiveLayer(id, quiet) {
    var layer = layerById(id);
    if (!layer || id === activeId) return;
    // Changer de couche pendant une prise ferait écrire le stylet dans le
    // canevas qu'un rejeu est en train de reconstruire, et la durée de la
    // prise atterrirait sur la mauvaise couche.
    if (recording) { status('Terminer l’enregistrement avant de changer de couche.'); return; }
    activeId = id;
    // On ne dessine pas à l'aveugle : rendre une couche active la montre.
    layer.visible = true;
    bindActiveLayer();
    syncLayerPanel();
    requestRedraw();
    updateExportState();
    if (!quiet) {
      publishLayers();
      status('Couche active : ' + activeLayer().name + '.');
    }
  }

  /** Repart d'une seule couche vide. Le format de travail a changé, ou un
   *  projet se charge : les traces déjà posées n'ont plus de référentiel. */
  function resetLayers() {
    layerSeq = 0;
    layers = [makeLayer()];
    activeId = layers[0].id;
    bindActiveLayer();
    t0 = null;
    lastElapsed = 0;
    recordedDuration = 0;
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    syncLayerPanel();
  }

  function resizeLayers() {
    layers.forEach(function (layer) {
      layer.canvas.width = config.width;
      layer.canvas.height = config.height;
    });
  }

  /** Vide la couche active — ses traces, ses effacements, son historique.
   *  Les couches verrouillées ne bougent pas : c'est tout l'intérêt. */
  function clearActiveLayer() {
    var layer = activeLayer();
    if (!layer) return;
    layer.strokes = [];
    layer.clears = [];
    layer.acts = [];
    layer.undone = [];
    layer.durationMs = 0;
    layer.ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    t0 = null;
    lastElapsed = 0;
    // Sans ça, une prise de trente secondes effacée laissait un plancher de
    // trente secondes : le dessin suivant, long de deux, s'exportait en trente.
    recordedDuration = 0;
    syncLayerPanel();
    requestRedraw();
    updateExportState();
  }

  /** Reconstruit le canevas d'une couche à partir de ses métadonnées. */
  var rebuildBuffer = null;

  function rebuildLayer(layer) {
    layer.ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    // Un effacement remet la couche à zéro : seul ce qui suit le dernier compte.
    var lastClear = layer.clears.length ? Math.max.apply(null, layer.clears) : -Infinity;
    if (!rebuildBuffer) rebuildBuffer = document.createElement('canvas');
    layer.strokes.forEach(function (item) {
      var points = item.points.filter(function (point) { return point.t >= lastClear; });
      if (!points.length) return;
      var trace = makeTrace({ brush: item.brush, seed: item.seed, points: points },
                            rebuildBuffer);
      feedTrace(trace, Infinity);
      commitTrace(trace, layer.ctx);
    });
  }

  /* -------------------------------------------------------------- Rejeu */

  /* Un rejeu rejoue des couches dans leur propre canevas en avançant dans le
   * temps. La prévisualisation le fait sur toutes ; le REC sur toutes sauf
   * l'active — c'est ce qui fait apparaître la couche 1, animée et calée sur
   * le média, sous le stylet qui dessine la couche 2. Sans ça, impossible de
   * poser un trait au bon moment sur une prise déjà faite. */

  function makeReplay(list) {
    return list.map(function (layer) {
      return {
        layer: layer,
        queue: layer.strokes.slice().sort(function (a, b) {
          return a.points[0].t - b.points[0].t;
        }),
        clears: layer.clears.slice().sort(function (a, b) { return a - b; }),
        active: []
      };
    });
  }

  /** Avance un rejeu. Retourne true si quelque chose a bougé — une trace
   *  posée, un effacement, une trace terminée. Le reste du temps il n'y a rien
   *  à recomposer : pendant le REC, la plupart des images ne voient rien
   *  arriver dans les couches du dessous, et recomposer la pile a chacune
   *  coûterait du temps au stylet qui, lui, dessine. */
  function feedReplay(list, elapsedMs) {
    var moved = false;
    list.forEach(function (track) {
      // Un effacement ne remet à zéro que SA couche ; les autres continuent.
      while (track.clears.length && track.clears[0] <= elapsedMs) {
        track.clears.shift();
        track.layer.ctx.clearRect(0, 0, track.layer.canvas.width, track.layer.canvas.height);
        track.active.forEach(function (trace) {
          trace.ctx.clearRect(0, 0, trace.canvas.width, trace.canvas.height);
        });
        moved = true;
      }
      while (track.queue.length && track.queue[0].points[0].t <= elapsedMs) {
        track.active.push(makeTrace(track.queue.shift()));
        moved = true;
      }
      for (var i = track.active.length - 1; i >= 0; i--) {
        var before = track.active[i].cursor;
        var done = feedTrace(track.active[i], elapsedMs);
        if (track.active[i].cursor !== before) moved = true;
        if (done) {
          commitTrace(track.active[i], track.layer.ctx);
          track.active.splice(i, 1);
          moved = true;
        }
      }
    });
    return moved;
  }

  function replayFinished(list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].queue.length || list[i].active.length) return false;
    }
    return true;
  }

  var NO_TRACE = [];

  /** Les traces d'une couche encore en cours d'écriture dans le rejeu. */
  function replayPending(layer) {
    if (!replay) return NO_TRACE;
    for (var i = 0; i < replay.length; i++) {
      if (replay[i].layer === layer) return replay[i].active;
    }
    return NO_TRACE;
  }

  function requestRedraw() {
    if (redrawPending) return;
    redrawPending = true;
    requestAnimationFrame(function () {
      redrawPending = false;
      redraw();
    });
  }

  /** Compose un calque de trace (déjà rasterisé) sur un contexte. */
  function composite(target, source, brush) {
    target.save();
    target.globalAlpha = brush.opacity;
    if (brush.eraser) target.globalCompositeOperation = 'destination-out';
    target.drawImage(source, 0, 0);
    target.restore();
  }

  /* L'empilement à l'écran est celui des couches, et ce qui n'est pas encore
   * fusionné se compose juste au-dessus de SA couche, jamais au-dessus de la
   * pile : une trace de la couche 1 reste sous la couche 2, y compris pendant
   * qu'elle s'écrit. C'est la seule chose qui distingue de vraies couches d'un
   * empilement décoratif — et `renderer.py` fait exactement la même. */
  function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    var current = activeLayer();
    layers.forEach(function (layer) {
      if (!layer.visible) return;
      // Une couche vide n'a rien à composer : ajouter une couche ne doit pas
      // coûter une image entière de recopie à chaque déplacement du stylet.
      if (layer.strokes.length || layer === current) ctx.drawImage(layer.canvas, 0, 0);
      replayPending(layer).forEach(function (trace) {
        composite(ctx, trace.canvas, trace.brush);
      });
      if (layer !== current) return;
      if (stroke) composite(ctx, scratchCanvas, stroke.brush);
      // Traces en cours d'arrivee de la tablette. Comme le trace local, chacun
      // vit dans son propre calque jusqu'a sa fin : c'est ce qui rend son
      // opacite globale et sa gomme corrects, au lieu de cumuler empreinte par
      // empreinte.
      for (var id in incoming) {
        if (Object.prototype.hasOwnProperty.call(incoming, id)) {
          composite(ctx, incoming[id].canvas, incoming[id].brush);
        }
      }
    });
  }

  function addPoint(event, isFirst) {
    var position = pointerPosition(event);
    var point = { x: position.x, y: position.y, t: elapsed(), p: pressureOf(event) };
    stroke.points.push(point);
    if (isFirst) stamper.begin(point); else stamper.extend(point);
    return point;
  }

  function onPointerDown(event) {
    // `viewing` : le poste regarde la tablette travailler. Son propre stylet
    // ne doit plus rien poser, sinon deux traces se melangeraient dans le
    // meme enregistrement sans qu'aucun des deux ecrans ne le montre.
    if (!ready || viewing || drawing || previewing) return;
    // Un pincement est en cours : le second doigt navigue, il ne pose rien.
    if (pinch) return;

    if (desktopShortcutsEnabled()) {
      if (shortcutState.space && event.button === 0) { startSpacePan(event); return; }
      if (event.altKey && event.button === 0) { if (startBrushShortcut(event, 'size')) return; }
      if (event.altKey && event.button === 2) { if (startBrushShortcut(event, 'hardness')) return; }
      if (shortcutState.opacity && event.button === 0 && !event.altKey) {
        if (startBrushShortcut(event, 'opacity')) return;
      }
      if ((event.ctrlKey || event.metaKey) && event.button === 0 && !event.altKey && !shortcutState.opacity) {
        startZoomShortcut(event);
        return;
      }
    }

    if (event.button > 0) return;
    canvas.setPointerCapture(event.pointerId);
    drawing = true;
    strokePointer = event.pointerType || 'mouse';
    setTabletDrawingState(true);

    var brush = shortcutBrush();
    var seed = (Math.random() * 0xffffffff) >>> 0;
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    stamper = new BE.StrokeStamper(brush, seed, scratchCtx);
    stroke = { brush: brush, seed: seed, points: [] };

    var point = addPoint(event, true);
    if (streaming()) {
      sendingStroke = 's' + (++strokeSeq);
      pointBuffer = [];
      // La graine part avec le trace : le poste rejoue le meme tirage
      // aleatoire, donc la meme dispersion et les memes taches.
      REMOTE.send({ t: 'stroke.begin', id: sendingStroke, brush: brush, seed: seed, point: point });
    }
    requestRedraw();
  }

  function onPointerMove(event) {
    if (shortcutState.pointer && event.pointerId === shortcutState.pointer.id) {
      event.preventDefault();
      var delta = shortcutDelta(event);
      if (shortcutState.pointer.mode === 'brush') {
        var param = PARAM_BY_KEY[shortcutState.pointer.key];
        var value = adjustedShortcutValue(param, shortcutState.pointer.startValue, delta);
        setPath(currentBrush, shortcutState.pointer.key, value);
        onBrushChanged();
        showShortcutHud(shortcutState.pointer.label + ' : ' + formatShortcutValue(param, value), true);
      } else if (shortcutState.pointer.mode === 'zoom') {
        zoomAt(shortcutState.pointer.anchorX, shortcutState.pointer.anchorY,
          shortcutState.pointer.startZoom * Math.exp(delta * 0.01));
        showShortcutHud('Zoom : ' + Math.round(displayScale() * 100) + ' %', true);
      }
      return;
    }
    if (!drawing) return;
    event.preventDefault();
    // `getCoalescedEvents` restitue les points que le navigateur a regroupes
    // entre deux images : un stylet echantillonne bien plus vite que l'ecran
    // ne rafraichit, et ce sont ces points-la qui font la finesse du trace.
    var events = event.getCoalescedEvents ? event.getCoalescedEvents() : [event];
    if (!events.length) events = [event];
    events.forEach(function (item) {
      var point = addPoint(item, false);
      if (streaming()) pointBuffer.push(point);
    });
    flushPoints();
    requestRedraw();
  }

  function onPointerUp(event) {
    if (shortcutState.pointer && event.pointerId === shortcutState.pointer.id) {
      clearShortcutPointer(event);
      return;
    }
    if (!drawing) return;
    drawing = false;
    setTabletDrawingState(false);
    try { canvas.releasePointerCapture(event.pointerId); } catch (e) { /* déjà relâché */ }

    if (stroke.points.length) {
      recordStroke(activeLayer(), stroke);
      baseCtx.save();
      baseCtx.globalAlpha = stroke.brush.opacity;
      if (stroke.brush.eraser) baseCtx.globalCompositeOperation = 'destination-out';
      baseCtx.drawImage(scratchCanvas, 0, 0);
      baseCtx.restore();
    }

    if (streaming() && sendingStroke) {
      flushPoints();
      REMOTE.send({ t: 'stroke.end', id: sendingStroke });
      sendingStroke = null;
    }

    stroke = null;
    stamper = null;
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    if (isTablet) wakeTabletBubble('tablet-control-bubble');
    requestRedraw();
    updateExportState();
  }

  /** Cet écran tient-il le stylet ?
   *
   *  Hors session partagée la question ne se pose pas : le poste fait tout.
   *  En session, un seul des deux dessine à la fois, et c'est le serveur qui
   *  l'arbitre — les deux clients lisent la même réponse, donc ils ne peuvent
   *  pas se croire tous les deux maîtres. */
  function inControl() {
    if (sessionMode !== 'live') return !isTablet;
    return controller === (isTablet ? 'tablet' : 'pc');
  }

  /** Ce que fait cet écran part-il vers l'autre ? Seul le maître émet : c'est
   *  ce qui interdit à un aller-retour de reboucler (le suiveur applique, il
   *  ne renvoie jamais). */
  function streaming() {
    return sessionMode === 'live' && inControl() && REMOTE.isOnline();
  }

  function flushPoints() {
    if (!sendingStroke || !pointBuffer.length) return;
    REMOTE.send({ t: 'stroke.points', id: sendingStroke, points: pointBuffer });
    pointBuffer = [];
  }

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('pointercancel', onPointerUp);
  canvas.addEventListener('contextmenu', function (event) { event.preventDefault(); });

  /* ------------------------------------------ Empreinte sous le curseur */

  /* Le curseur système est remplacé par l'empreinte réelle du pinceau :
   * forme, taille, angle, couleur et opacité, à l'échelle du canevas. */

  /** Combien de pixels de canevas vaut un pixel écran (suit le zoom). */
  function canvasPerScreenPixel() {
    var rect = canvas.getBoundingClientRect();
    return rect.width ? canvas.width / rect.width : 1;
  }

  function scheduleCursor() {
    if (cursor.pending) return;
    cursor.pending = true;
    requestAnimationFrame(function () {
      cursor.pending = false;
      drawCursorPreview();
    });
  }

  var cursorStamp = { key: null, canvas: null, half: 0 };

  /** Fabrique l'empreinte isolée du pinceau, telle qu'elle sera déposée. */
  function buildCursorStamp(brush) {
    var key = [brush.shape, brush.size, brush.hardness, brush.aspect, brush.angle,
      brush.color, brush.flow, brush.eraser, brush.texture || ''].join('|');
    if (cursorStamp.key === key) return cursorStamp;

    var preview = JSON.parse(JSON.stringify(brush));
    // Ni dispersion ni pression : sous la main, le curseur doit rester stable.
    preview.jitter = { position: 0, size: 0, angle: 0, opacity: 0 };
    preview.pressure = { size: 0, opacity: 0 };
    // Une gomme n'a pas de couleur : on montre un retrait, en clair.
    if (preview.eraser) preview.color = '#ffffff';

    // Marge : un rectangle tourné déborde du carré de la taille nominale.
    var half = Math.ceil(brush.size * 0.75) + 4;
    var scratch = document.createElement('canvas');
    scratch.width = scratch.height = half * 2;
    new BE.StrokeStamper(preview, 7, scratch.getContext('2d'))
      .begin({ x: half, y: half, p: 1 });

    cursorStamp = { key: key, canvas: scratch, half: half };
    return cursorStamp;
  }

  /* Le curseur montre le *résultat* du pinceau, pas seulement son gabarit :
   * le surligneur pose un rectangle jaune translucide, le feutre un point noir
   * opaque avec sa dureté. Le contour reste fin et discret — il situe la forme
   * sans masquer l'aperçu. */
  function drawCursorPreview() {
    if (!overlay.width) return;
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    if (!cursor.visible || !currentBrush || previewing) return;

    var brush = shortcutBrush();
    var unit = canvasPerScreenPixel();
    var stamp = buildCursorStamp(brush);

    overlayCtx.save();
    overlayCtx.globalAlpha = brush.eraser ? 0.4 : brush.opacity;
    overlayCtx.drawImage(stamp.canvas, cursor.x - stamp.half, cursor.y - stamp.half);
    overlayCtx.restore();

    var half = Math.max(brush.size, unit * 3) / 2;
    overlayCtx.save();
    overlayCtx.translate(cursor.x, cursor.y);
    overlayCtx.rotate(brush.angle * Math.PI / 180);
    overlayCtx.beginPath();
    if (brush.shape === 'rect') {
      overlayCtx.rect(-half, -half * brush.aspect, half * 2, half * 2 * brush.aspect);
    } else {
      overlayCtx.arc(0, 0, half, 0, Math.PI * 2);
    }
    if (brush.eraser) overlayCtx.setLineDash([unit * 5, unit * 4]);
    overlayCtx.lineWidth = unit * 2;
    overlayCtx.strokeStyle = 'rgba(255,255,255,.3)';
    overlayCtx.stroke();
    overlayCtx.lineWidth = unit;
    overlayCtx.strokeStyle = 'rgba(0,0,0,.42)';
    overlayCtx.stroke();
    overlayCtx.restore();
  }

  /* Pendant un réglage au glisser, l'empreinte reste là où le geste a commencé.
   *
   * C'est elle qu'on est en train de juger : la voir traverser l'écran pendant
   * qu'elle change empêche précisément de lire le changement. Ce qui est cloué
   * est donc le *dessin* de l'empreinte, pas la souris.
   *
   * Le verrou de pointeur, essayé d'abord, réglait le symptôme au prix de deux
   * défauts : Chrome l'annonce par un bandeau « appuyez sur Échap pour afficher
   * le curseur » en travers de l'écran, et macOS ne l'accorde pas de façon
   * fiable. Figer le rendu ne demande aucune permission et se comporte partout
   * de la même façon ; la capture de pointeur, elle, suffit déjà à ce que le
   * geste survive à la sortie du canevas. */
  function moveCursor(event) {
    if (adjustingBrush()) return;
    var position = pointerPosition(event);
    cursor.x = position.x;
    cursor.y = position.y;
    cursor.visible = true;
    canvas.classList.add('brush-cursor');
    scheduleCursor();
  }

  function hideCursor() {
    // Sortir du canevas en plein réglage ne doit pas escamoter l'empreinte :
    // le geste, lui, continue grâce à la capture de pointeur.
    if (adjustingBrush()) return;
    cursor.visible = false;
    canvas.classList.remove('brush-cursor');
    scheduleCursor();
  }

  canvas.addEventListener('pointermove', moveCursor);
  canvas.addEventListener('pointerenter', moveCursor);
  canvas.addEventListener('pointerleave', hideCursor);

  /* -------------------------------------------------- Zoom et navigation */

  /** Échelle réelle affichée : 100 % = un pixel de canevas pour un pixel écran. */
  function displayScale() {
    return config.width ? (fit.width * view.zoom) / config.width : view.zoom;
  }

  /** Débattement disponible : zéro tant que l'image tient dans le cadre. */
  function panLimits() {
    var workspace = $('workspace');
    return {
      x: Math.max(0, (fit.width * view.zoom - workspace.clientWidth) / 2),
      y: Math.max(0, (fit.height * view.zoom - workspace.clientHeight) / 2)
    };
  }

  function applyView() {
    var container = $('media-container');
    var limits = panLimits();
    // On ne peut pas pousser l'image hors du cadre : le débattement s'arrête
    // dès que son bord atteint celui du plan de travail.
    view.panX = clamp(view.panX, -limits.x, limits.x);
    view.panY = clamp(view.panY, -limits.y, limits.y);

    // Cadre calé sur des pixels entiers : une largeur fractionnaire fait
    // diverger l'arrondi du fond blanc et celui du média posé dessus.
    container.style.width = Math.round(fit.width * view.zoom) + 'px';
    container.style.height = Math.round(fit.height * view.zoom) + 'px';
    container.style.left = Math.round(view.panX) + 'px';
    container.style.top = Math.round(view.panY) + 'px';

    $('zoom-level').textContent = Math.round(displayScale() * 100) + ' %';
    syncPanSliders(limits);
    scheduleCursor();
  }

  /* Les curseurs de défilement n'apparaissent que s'il y a de quoi défiler.
   * Leur valeur suit directement le décalage affiché. */
  /* Le quatrième terme est le sens du curseur par rapport au décalage de vue.
   *
   * `panY` positif descend le conteneur, ce qui découvre le *haut* de l'image.
   * Or le curseur vertical a son minimum en haut : le descendre augmentait donc
   * `panY` et montrait le haut, à rebours de toute barre de défilement. Il va
   * désormais à l'inverse de la valeur de vue. L'axe horizontal, lui, tombe
   * déjà juste : le pousser à droite montre la droite de l'image. */
  function syncPanSliders(limits) {
    [['pan-x', 'x', 'panX', 1], ['pan-y', 'y', 'panY', -1]].forEach(function (entry) {
      var slider = $(entry[0]);
      var limit = limits[entry[1]];
      slider.hidden = limit < 1;
      if (slider.hidden) return;
      slider.min = -limit;
      slider.max = limit;
      slider.step = 1;
      if (document.activeElement !== slider) slider.value = view[entry[2]] * entry[3];
    });
  }

  $('pan-x').addEventListener('input', function () {
    view.panX = parseFloat($('pan-x').value);
    applyView();
  });

  $('pan-y').addEventListener('input', function () {
    view.panY = -parseFloat($('pan-y').value);
    applyView();
  });

  function zoomAt(clientX, clientY, target) {
    var next = clamp(target, ZOOM_MIN, ZOOM_MAX);
    var rect = $('workspace').getBoundingClientRect();
    var cx = clientX - rect.left - rect.width / 2;
    var cy = clientY - rect.top - rect.height / 2;
    var scale = next / view.zoom;
    // Le point sous le curseur ne doit pas bouger pendant le zoom.
    view.panX = cx - (cx - view.panX) * scale;
    view.panY = cy - (cy - view.panY) * scale;
    view.zoom = next;
    applyView();
  }

  function zoomByStep(factor) {
    var rect = $('workspace').getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, view.zoom * factor);
  }

  function fitView() {
    view.zoom = 1;
    view.panX = 0;
    view.panY = 0;
    applyView();
  }

  $('zoom-in').addEventListener('click', function () { zoomByStep(1.25); });
  $('zoom-out').addEventListener('click', function () { zoomByStep(1 / 1.25); });
  $('zoom-fit').addEventListener('click', fitView);
  // Le bandeau se déplie au survol ; le clic l'épingle (utile au stylet).
  $('zoom-loupe').addEventListener('click', function () {
    $('zoom-controls').classList.toggle('open');
  });

  /* Pincement du pavé tactile (que le navigateur signale par `ctrlKey`) et
   * Ctrl + molette zooment ; le glisser à deux doigts, lui, déplace l'image. */
  $('workspace').addEventListener('wheel', function (event) {
    if (!ready) return;
    event.preventDefault();

    if (event.ctrlKey || event.metaKey) {
      var step = clamp(event.deltaY, -60, 60);
      zoomAt(event.clientX, event.clientY, view.zoom * Math.exp(-step * 0.007));
      return;
    }

    // deltaMode 1 = défilement exprimé en lignes, pas en pixels.
    var unit = event.deltaMode === 1 ? 16 : 1;
    view.panX -= event.deltaX * unit;
    view.panY -= event.deltaY * unit;
    applyView();
  }, { passive: false });

  /* ------------------------------------------------- Pincement tactile */

  /* Sur un poste, le pincement du pavé tactile arrive déjà cuit : le
   * navigateur le présente comme une molette avec `ctrlKey`. Sur une tablette,
   * un pincement est ce qu'il est — deux doigts —, et rien ne l'annonce : il
   * faut le suivre doigt par doigt. C'est là qu'il manquait, et c'est là qu'il
   * sert le plus : la tablette n'a ni molette, ni clavier pour `Ctrl + +`.
   *
   * Deux doigts veulent naviguer, pas dessiner : le trait que le premier avait
   * commencé est abandonné — jamais enregistré, jamais exporté. Le stylet,
   * lui, n'est pas interrompu : une paume posée à côté ne doit pas lui couper
   * son geste. */

  var touching = {};   /* pointerId tactile -> dernière position connue */
  var pinch = null;    /* écart et milieu des deux doigts, à la dernière image */

  function touchCount() {
    return Object.keys(touching).length;
  }

  function pinchGeometry() {
    var ids = Object.keys(touching);
    var a = touching[ids[0]];
    var b = touching[ids[1]];
    var dx = b.x - a.x;
    var dy = b.y - a.y;
    return {
      // Jamais zéro : deux doigts parfaitement superposés diviseraient par lui.
      spread: Math.max(1, Math.sqrt(dx * dx + dy * dy)),
      x: (a.x + b.x) / 2,
      y: (a.y + b.y) / 2
    };
  }

  /** Abandonne le trait en cours sans l'enregistrer. */
  function abortStroke() {
    if (!drawing) return;
    drawing = false;
    setTabletDrawingState(false);
    stroke = null;
    stamper = null;
    scratchCtx.clearRect(0, 0, scratchCanvas.width, scratchCanvas.height);
    if (streaming() && sendingStroke) {
      // L'autre écran a vu le trait naître : il doit le voir disparaître,
      // sinon son calque resterait à l'écran sans jamais être fusionné.
      REMOTE.send({ t: 'stroke.abort', id: sendingStroke });
      sendingStroke = null;
    }
    requestRedraw();
  }

  $('workspace').addEventListener('pointerdown', function (event) {
    if (event.pointerType !== 'touch') return;
    touching[event.pointerId] = { x: event.clientX, y: event.clientY };
    if (touchCount() !== 2) return;
    // Un trait au stylet n'est jamais interrompu : la paume et un doigt posés
    // à côté ne doivent pas lui couper son geste. Le pincement attendra que
    // le stylet soit relevé — il ne commence même pas.
    if (drawing && strokePointer === 'pen') return;
    // En phase de capture : le garde de `onPointerDown` lit `pinch` juste
    // après, et le second doigt ne commence donc aucun trait.
    abortStroke();
    stopPanning();
    pinch = pinchGeometry();
  }, true);

  window.addEventListener('pointermove', function (event) {
    if (event.pointerType !== 'touch' || !touching[event.pointerId]) return;
    touching[event.pointerId].x = event.clientX;
    touching[event.pointerId].y = event.clientY;
    if (!pinch || touchCount() !== 2) return;
    event.preventDefault();

    var now = pinchGeometry();
    zoomAt(now.x, now.y, view.zoom * (now.spread / pinch.spread));
    // Les deux doigts déplacent aussi : un pincement qui ne ferait que zoomer
    // laisserait le détail visé hors du cadre une fois sur deux.
    view.panX += now.x - pinch.x;
    view.panY += now.y - pinch.y;
    applyView();
    pinch = now;
  }, { passive: false });

  ['pointerup', 'pointercancel'].forEach(function (type) {
    window.addEventListener(type, function (event) {
      if (event.pointerType !== 'touch') return;
      delete touching[event.pointerId];
      // Un doigt levé sur trois laisse un pincement valable : on repart du
      // nouvel écart plutôt que de faire sauter l'image.
      pinch = touchCount() === 2 ? pinchGeometry() : null;
    });
  });

  /* Clic molette : déplacement de la vue, sans jamais dessiner. */
  $('workspace').addEventListener('pointerdown', function (event) {
    if (event.button !== 1) return;
    event.preventDefault();
    panning = { x: event.clientX, y: event.clientY };
    $('media-container').classList.add('panning');
  });

  window.addEventListener('pointermove', function (event) {
    if (!panning) return;
    view.panX += event.clientX - panning.x;
    view.panY += event.clientY - panning.y;
    panning.x = event.clientX;
    panning.y = event.clientY;
    applyView();
  });

  ['pointerup', 'pointercancel'].forEach(function (type) {
    window.addEventListener(type, function () {
      if (!panning) return;
      stopPanning();
    });
  });

  function isTyping(target) {
    return target && (target.tagName === 'INPUT' || target.tagName === 'SELECT'
      || target.tagName === 'TEXTAREA');
  }

  function syncShortcutKeys(event) {
    shortcutState.alt = !!event.altKey;
    shortcutState.ctrl = !!event.ctrlKey;
    shortcutState.meta = !!event.metaKey;
  }

  function releaseShortcutKeys() {
    shortcutState.alt = false;
    shortcutState.ctrl = false;
    shortcutState.meta = false;
    shortcutState.space = false;
    shortcutState.opacity = false;
    shortcutState.eraser = false;
    shortcutState.spaceConsumed = false;
    syncPanel();
  }

  window.addEventListener('keydown', function (event) {
    if (!ready || isTyping(event.target)) return;
    syncShortcutKeys(event);

    if (!isTablet && event.ctrlKey && event.altKey && event.code === 'KeyE') {
      event.preventDefault();
      openExportDialog();
      return;
    }

    if (!isTablet && event.code === 'KeyO') {
      shortcutState.opacity = true;
      return;
    }

    if (!isTablet && event.code === 'KeyE' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      shortcutState.eraser = true;
      syncPanel();
      return;
    }

    if (!isTablet && event.code === 'Space') {
      event.preventDefault();
      if (!event.repeat) shortcutState.spaceConsumed = false;
      shortcutState.space = true;
      return;
    }

    // Annuler / rétablir. Disponible aussi sur la tablette : c'est elle qui
    // dessine, c'est elle qui doit pouvoir revenir en arrière.
    if ((event.ctrlKey || event.metaKey) && (event.code === 'KeyZ' || event.code === 'KeyY')) {
      event.preventDefault();
      if (previewing) return;
      if (viewing) { status(handOverNotice('annuler')); return; }
      if (event.code === 'KeyY' || event.shiftKey) redo(); else undo();
      return;
    }

    if (event.ctrlKey || event.metaKey) {
      if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomByStep(1.25); }
      else if (event.key === '-') { event.preventDefault(); zoomByStep(1 / 1.25); }
      else if (event.key === '0') { event.preventDefault(); fitView(); }
      return;
    }

    if (event.code === 'Space' && window.MediaTransport && MediaTransport.isTimed()) {
      event.preventDefault();
      MediaTransport.togglePlay();
    }
  });

  window.addEventListener('keyup', function (event) {
    syncShortcutKeys(event);
    if (isTyping(event.target)) return;

    if (!isTablet && event.code === 'KeyO') {
      shortcutState.opacity = false;
      return;
    }

    if (!isTablet && event.code === 'KeyE') {
      shortcutState.eraser = false;
      syncPanel();
      return;
    }

    if (!isTablet && event.code === 'Space') {
      event.preventDefault();
      shortcutState.space = false;
      if (!shortcutState.spaceConsumed && window.MediaTransport && MediaTransport.isTimed()) {
        MediaTransport.togglePlay();
      }
      shortcutState.spaceConsumed = false;
    }
  });

  window.addEventListener('blur', function () {
    releaseShortcutKeys();
    if (shortcutState.pointer) clearShortcutPointer();
    stopPanning();
  });

  function updateExportState() {
    // Tant que la tablette a la main, l'export attend : c'est elle qui décide
    // quand le tracé est terminé, et le poste ne reprend qu'ensuite.
    var usable = visibleStrokeCount() > 0 && !recording && !previewing && !viewing;
    $('btn-export').disabled = !usable;
    $('btn-preview').disabled = !usable;
    syncHistoryButtons();
    if (strokeCount() && !previewing) status(drawingSummary());
  }

  /** Ce que la ligne d'état dit du dessin. Une couche masquée y est nommée :
   *  elle ne s'exporte pas, et le découvrir au fichier livré serait tard. */
  function drawingSummary() {
    var current = activeLayer();
    var hidden = layers.filter(function (layer) { return !layer.visible; }).length;
    var text = (current ? current.name + ' — ' + current.strokes.length + ' tracé(s). ' : '');
    if (layers.length > 1) {
      text += layers.length + ' couches, ' + strokeCount() + ' tracés au total — ';
    }
    text += (duration() / 1000).toFixed(1) + ' s.';
    if (hidden) text += ' ' + hidden + ' couche(s) masquée(s), exclue(s) de l’export.';
    return text;
  }

  /** Durée du rendu : celle de ce qui sortira réellement du tuyau. Une couche
   *  masquée n'y entre pas — sans quoi une prise de soixante secondes qu'on a
   *  masquée imposerait sa longueur à l'annotation de deux qui la remplace, et
   *  le fichier livré serait fait de cinquante-huit secondes de vide. */
  function duration() {
    var last = 0;
    layers.forEach(function (layer) {
      if (!layer.visible) return;
      last = Math.max(last, layer.durationMs || 0);
      layer.strokes.forEach(function (item) {
        var points = item.points;
        if (points.length) last = Math.max(last, points[points.length - 1].t);
      });
    });
    return last;
  }

  /* ------------------------------------------------------------ Setup */

  $('btn-create').addEventListener('click', function () {
    createWorkspace({
      width: parseInt($('setup-width').value, 10) || 1920,
      height: parseInt($('setup-height').value, 10) || 1080,
      fps: parseInt($('setup-fps').value, 10) || 30,
      alpha: $('setup-alpha').checked
    });
    // La tablette travaille dans le meme referentiel : sans cela, un point
    // pose a (x, y) chez elle ne tomberait pas au meme endroit chez le poste.
    publishConfig();
  });

  /** Installe la zone de dessin. Appele par le poste depuis l'ecran de
   *  configuration, et par la tablette a partir de l'etat publie par le poste. */
  function createWorkspace(next) {
    config.width = next.width;
    config.height = next.height;
    config.fps = next.fps;
    config.alpha = !!next.alpha;

    [canvas, overlay, scratchCanvas].forEach(function (node) {
      node.width = config.width;
      node.height = config.height;
    });
    // Redimensionner un canevas le vide : c'est voulu, les traces qui s'y
    // trouvaient étaient exprimées dans l'ancien référentiel. Celui qui charge
    // un projet reconstruit derrière (`rebuildFinalCanvasAsync`).
    if (!layers.length) resetLayers(); else { resizeLayers(); bindActiveLayer(); }

    /* Le damier n'est plus une conséquence de l'export alpha mais un choix de
     * fond à part entière. On ne le retient d'office que pour un premier
     * lancement avec alpha coché, afin de ne rien changer à ce que les
     * utilisateurs connaissent. Le fond est posé par un `toggle` booléen, donc
     * la tablette peut rejouer `createWorkspace` à chaque configuration publiée
     * sans que le damier reste collé — et le fond publié par le poste l'emporte
     * sur la préférence locale de cette machine. */
    if (next.background) adoptBackground(next.background);
    else {
      if (readStoredBackground() === null && config.alpha) config.background = CHECKER;
      showBackground(config.background);
    }

    /* Replier une barre change la place disponible sans redimensionner la
     * fenêtre : c'est le plan de travail lui-même qu'il faut observer, sinon
     * l'image reste à sa taille et laisse un vide. */
    if (!observingWorkspace) {
      observingWorkspace = true;
      if (window.ResizeObserver) {
        new ResizeObserver(resizeWorkspace).observe($('workspace'));
      } else {
        window.addEventListener('resize', resizeWorkspace);
      }
    }
    resizeWorkspace();
    $('setup-screen').style.display = 'none';
    ready = true;
    status('Espace prêt — ' + config.width + '×' + config.height + ' @ ' + config.fps + ' FPS.');

    // Un média choisi avant la création de l'espace a été rattaché avec le
    // FPS par défaut : le transport doit repartir sur la bonne cadence, sans
    // quoi les timecodes affichés seraient faux. Sa géométrie, elle, a été
    // calculée contre le canevas par défaut — d'ou ce second passage, une fois
    // le format réel connu. Il vient après `ready` et après l'effacement de
    // l'écran de configuration : la question du cadrage ne se pose qu'a ce
    // moment-là, sinon la fenêtre s'ouvrirait par-dessus la configuration et
    // porterait sur un format que l'utilisateur.ice n'a pas encore choisi.
    // Les bornes déjà posées passent le pas : refaire l'espace de travail
    // change la cadence, pas la portion du rush que l'on annote. Sans cela,
    // la tablette qui adoptait le format du poste réinitialisait sa plage,
    // l'annonçait — et effaçait celle du poste au passage.
    if (mediaNode) {
      var kept = currentRange();
      MediaTransport.attach(mediaNode, { kind: mediaType, fps: config.fps, name: mediaName,
                                         onRangeChange: onRangeChanged,
                                         inPoint: kept ? kept.in : undefined,
                                         outPoint: kept ? kept.out : undefined });
      onMediaReady();
    }
    // La cadence du projet vient de changer : l'écart avec celle du rush aussi.
    syncCadenceWarning();
  }

  /** Bornes actuellement posées, ou `null` quand il n'y en a pas de sensées —
   *  pas de média cadençable, ou durée encore inconnue. */
  function currentRange() {
    if (!MediaTransport.isTimed()) return null;
    var end = MediaTransport.outPoint();
    var start = MediaTransport.inPoint();
    return isFinite(end) && end > start ? { in: start, out: end } : null;
  }

  var observingWorkspace = false;

  function publishConfig() {
    if (isTablet) return;
    // Le fond et le cadrage voyagent avec le format : ils décrivent ce que le
    // canevas montre sous le tracé, et deux écrans qui ne les partagent pas
    // annotent une image qui n'est pas à la même place.
    var payload = {
      width: config.width, height: config.height, fps: config.fps,
      alpha: config.alpha, background: config.background, mediaFit: mediaFit
    };
    // Les bornes IN/OUT suivent le même chemin : elles disent quand le tracé
    // commence, et une tablette qui rejoint en cours de route doit les
    // retrouver telles quelles — sinon elle annote le même rush sur un autre
    // minutage.
    if (MediaTransport.isTimed()) {
      payload.inPoint = MediaTransport.inPoint();
      payload.outPoint = MediaTransport.outPoint();
    }
    fetch('/api/session/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(function () { /* le mode tablette n'est peut-être pas utilisé */ });
  }


  /** Média choisi localement dans le navigateur (repli quand le sélecteur
   *  natif du serveur n'est pas disponible). Le navigateur ne sait lire que
   *  les codecs qu'il connaît : c'est exactement la limite que lève le
   *  passage par le serveur (`openMediaOnServer`). */
  function loadMedia(file) {
    if (!file) return;
    var kind = file.type.indexOf('video/') === 0 ? 'video'
      : file.type.indexOf('image/') === 0 ? 'image'
        : file.type.indexOf('audio/') === 0 ? 'audio' : null;
    if (!kind) {
      // Un PDF a besoin d'être rasterisé côté serveur : il n'y a rien à faire
      // d'un blob anonyme, et le repli navigateur ne peut pas le prendre.
      status(file.type === 'application/pdf'
        ? 'Un PDF ne peut être ouvert que par le sélecteur natif du serveur.'
        : 'Type de média non reconnu : ' + (file.type || file.name));
      return;
    }
    installMedia(URL.createObjectURL(file), file.name, kind, true);
  }

  /**
   * Défait l'installation du média : l'élément quitte le DOM, son URL objet est
   * révoquée — sans quoi le fichier reste en mémoire pour toute la session — et
   * le cadrage repart de zéro, celui du précédent n'ayant aucun sens sur une
   * autre image. Ne touche pas au transport : `installMedia()` le réattache
   * aussitôt.
   */
  function releaseMedia() {
    if (mediaNode) {
      if (mediaType !== 'image') mediaNode.pause();
      var placeholder = document.createElement('div');
      placeholder.id = 'bg-media';
      mediaNode.replaceWith(placeholder);
      // Une URL objet retient le fichier en mémoire tant qu'on ne la révoque
      // pas ; une URL servie par le serveur n'a rien à libérer.
      if (mediaUrl && mediaIsObjectUrl) URL.revokeObjectURL(mediaUrl);
    }
    mediaNode = null;
    mediaUrl = null;
    mediaIsObjectUrl = false;
    mediaType = null;
    mediaFit = { mode: 'contain', zoom: 1, posX: 0.5, posY: 0.5 };
    mediaSize = { width: 0, height: 0 };
    setMediaLoading(false);
    setMediaSourceBadge(null);
    syncCadenceWarning();
    $('tp-fit').style.display = 'none';
    $('tp-page').style.display = 'none';
    $('tp-clear').style.display = 'none';
    closeCrop();
    // Le cadrage vient d'être remis à zéro : la tablette doit l'apprendre,
    // sinon elle garderait celui du rush précédent sur le suivant.
    publishConfig();
  }

  /* ------------------------------------------------- Fond du canevas */

  /* Le fond est le papier : c'est sur lui qu'on juge un tracé, et il n'y a
   * aucune raison qu'il soit blanc d'office. Il descend aussi jusqu'au moteur
   * de rendu, qui aplatit dessus les exports sans alpha. */
  var BG_KEY = 'live_notes.background.v1';

  function readStoredBackground() {
    try { return localStorage.getItem(BG_KEY); } catch (e) { return null; }
  }

  /** Seule entrée pour changer de fond : écrit l'état, persiste, resynchronise. */
  /** Fond choisi ici : on le retient pour les prochaines sessions et on
   *  l'annonce à la tablette, qui doit montrer le même papier. */
  function setBackground(value) {
    showBackground(value);
    try { localStorage.setItem(BG_KEY, value); } catch (e) { /* quota */ }
    publishConfig();
  }

  /** Fond arbitré par le poste. La tablette l'affiche sans l'inscrire dans ses
   *  préférences : elle n'a rien choisi, elle suit. */
  function adoptBackground(value) {
    showBackground(value);
  }

  function showBackground(value) {
    config.background = value;
    applyBackground();
    syncBackgroundControls();
  }

  function applyBackground() {
    var box = $('media-container');
    var checker = config.background === CHECKER;
    box.classList.toggle('bg-checker', checker);
    // Le style inline l'emporte sur la règle #media-container : c'est ce qui
    // permet de remplacer le blanc du papier sans toucher au CSS. Sur le damier
    // on y écrit le blanc des cases claires plutôt que de retirer la propriété :
    // sans lui, la couleur héritée du conteneur transparaît sous le motif.
    box.style.backgroundColor = checker ? '#ffffff' : config.background;
    // Le cadre d'aperçu du recadrage montre le même fond, sinon il mentirait
    // sur ce que donne le mode « ratio d'origine ».
    var stage = $('crop-stage');
    stage.classList.toggle('bg-checker', checker);
    stage.style.backgroundColor = checker ? '#ffffff' : config.background;
  }

  function syncBackgroundControls() {
    var checker = config.background === CHECKER;
    // Un <input type="color"> n'accepte que du hex : sur le damier on lui
    // laisse sa dernière couleur plutôt que de le forcer à une valeur fausse.
    if (!checker) {
      $('bg-color').value = config.background;
      $('setup-bg-color').value = config.background;
    }
    $('bg-checker-btn').classList.toggle('btn-strong', checker);
    Array.prototype.forEach.call(document.querySelectorAll('.swatch'), function (swatch) {
      swatch.classList.toggle('active', swatch.dataset.bg === config.background);
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('.swatch'), function (swatch) {
    swatch.addEventListener('click', function () { setBackground(swatch.dataset.bg); });
  });

  ['bg-color', 'setup-bg-color'].forEach(function (id) {
    $(id).addEventListener('input', function (event) { setBackground(event.target.value); });
  });

  $('bg-checker-btn').addEventListener('click', function () {
    // Bascule : un second clic rend la main à la couleur choisie.
    setBackground(config.background === CHECKER ? $('bg-color').value : CHECKER);
  });

  /** Installe (ou remplace) le média de fond. Rappelable à tout moment. */
  function installMedia(url, name, kind, isObjectUrl, range) {
    releaseMedia();

    mediaUrl = url;
    mediaIsObjectUrl = !!isObjectUrl;
    mediaType = kind;
    mediaNode = document.createElement(
      kind === 'video' ? 'video' : (kind === 'image' ? 'img' : 'audio'));

    mediaName = name;
    $('tp-clear').style.display = '';

    // Les contrôles natifs sont remplacés par le transport maison (transport.js).
    mediaNode.controls = false;
    if (kind !== 'image') {
      // Sans cela, iOS bascule la vidéo en lecteur plein écran natif dès le
      // play et l'on perd le canevas de dessin par-dessus.
      mediaNode.playsInline = true;
      mediaNode.setAttribute('playsinline', '');
      mediaNode.preload = 'auto';
    }
    mediaNode.src = url;
    mediaNode.id = 'bg-media';
    // La géométrie est reprise en main par `applyMediaFit()` dès l'arrivée des
    // métadonnées ; `fill` parce que l'élément est dimensionné au ratio du
    // média lui-même, il n'y a donc rien à ajuster à l'intérieur.
    mediaNode.style.objectFit = 'fill';
    $('bg-media').replaceWith(mediaNode);

    setMediaLoading(true);
    // Le transport prend la main *avant* que l'application n'écoute les
    // métadonnées : les écouteurs se déclenchent dans l'ordre où ils ont été
    // posés, et `onMediaReady` repose les bornes de la session. Branché en
    // premier, il les reposait contre une durée que le transport ne
    // connaissait pas encore.
    MediaTransport.attach(mediaNode, { kind: kind, fps: config.fps, name: name,
                                       onRangeChange: onRangeChanged,
                                       inPoint:  range ? range.in  : undefined,
                                       outPoint: range ? range.out : undefined });

    mediaNode.addEventListener(kind === 'image' ? 'load' : 'loadedmetadata', onMediaReady);
    mediaNode.addEventListener('error', function () {
      status('Média illisible par ce navigateur : « ' + name + ' ».');
      rescueUnreadableMedia();
    });

    // `attach` ne prévient personne des bornes qu'il pose — ce n'est pas lui
    // qui les décide. C'est ici, une fois la durée connue, que le poste les
    // publie : une tablette qui rejoint plus tard doit les lire, et une
    // tablette qui s'installe ne doit jamais pouvoir écraser celles du poste
    // avec la plage entière de son propre lecteur.
    if (!isTablet) {
      if (kind === 'image') publishConfig();
      else mediaNode.addEventListener('loadedmetadata', publishConfig, { once: true });
    }
    // Un lecteur qui vient de naître ne connaît pas encore son rôle : sans
    // cela, changer de rush pendant une session ferait repartir le son des
    // deux côtés à la fois.
    applyAudioRole();
    status('Média « ' + name + ' » chargé.');
    syncCadenceWarning();
  }

  /** « 24 », « 25 », mais « 23.98 » : une cadence entière se lit entière. */
  function formatFps(value) {
    return Math.abs(value - Math.round(value)) < 0.005
      ? String(Math.round(value)) : value.toFixed(2);
  }

  /* Cadence du rush contre cadence du projet.
   *
   * Les bornes IN/OUT se calent sur la grille d'images du **projet** : c'est
   * elle qui donne un sens à « une image », ici comme au rendu. Un rush qui n'a
   * pas cette cadence n'a aucune image aux instants où tombent les bornes — un
   * point OUT posé sur la dernière image d'un plan tombe au milieu d'une image
   * du rush. Celle qui se fige n'est alors pas tout à fait celle qu'on a
   * désignée, et l'écart se déplace d'une borne à l'autre : un rush 24 dans un
   * projet 25 dérive d'une image toutes les vingt-cinq.
   *
   * Rien n'est réparable en aval : c'est le projet qui n'est pas à la cadence
   * de son rush, et cela se décide au moment de le créer. Le seul service à
   * rendre est de le dire, et de le dire là où l'on regarde le rush. */
  function syncCadenceWarning() {
    var node = $('tp-cadence');
    if (!node) return;
    // Une image n'a pas de cadence, un son non plus, et un fichier ouvert
    // depuis le navigateur n'est pas passé par le serveur : sans mesure, on se
    // tait plutôt que d'inventer un écart.
    var rush = currentMedia && currentMedia.kind === 'video'
      ? Number(currentMedia.fps) || 0 : 0;
    // Le seuil laisse passer les cadences NTSC annoncées à la fraction près
    // (30000/1001 contre 30) et retient 24 contre 25.
    var apart = rush > 0 && Math.abs(rush - config.fps) > 0.05;
    node.style.display = apart ? '' : 'none';
    if (!apart) return;

    var said = 'Rush à ' + formatFps(rush) + ' i/s, projet à '
      + formatFps(config.fps) + ' i/s';
    node.textContent = '⚠ ' + said;
    node.title = said + '.\n\nLes points IN et OUT se calent sur les images du '
      + 'projet : à cette cadence-là, ils ne tombent pas sur des images du '
      + 'rush. L’image figée en fin de plage peut être décalée d’une image, et '
      + 'l’écart se déplace le long du rush.\n\nLa cadence du projet se choisit '
      + 'à sa création : pour annoter ce rush image par image, repartez d’un '
      + 'projet à ' + formatFps(rush) + ' i/s.';
    status(said + ' — les bornes IN/OUT ne tomberont pas sur des images du '
      + 'rush. Pour annoter image par image, repartez d’un projet à '
      + formatFps(rush) + ' i/s.');
  }

  /** Le navigateur vient de refuser un rush qu'on lui servait tel quel : c'est
   *  le seul avis qui compte vraiment.
   *
   *  Aucune analyse de fichier ne dit ce qu'une machine décode — cela dépend de
   *  la version du navigateur, du système, de la présence d'un décodeur
   *  matériel. Plutôt que d'élargir indéfiniment une liste de codecs qui sera
   *  toujours fausse quelque part, on sert d'abord la source et on ne transcode
   *  que si l'échec est constaté. Le verdict est prononcé par l'intéressé. */
  function rescueUnreadableMedia() {
    if (isTablet || !currentMedia || currentMedia.state !== 'ready') return;
    // Déjà transcodé : le refaire ne donnerait pas un fichier plus lisible.
    if (currentMedia.proxied || !currentMedia.sourcePath) return;
    // « Servir l'original » est une décision explicite : la défaire
    // automatiquement rendrait le bouton inopérant.
    if (currentMedia.bypassed) return;
    if (rescuedMediaKey === currentMedia.id) return;
    rescuedMediaKey = currentMedia.id;

    status('Ce navigateur ne lit pas « ' + currentMedia.name
      + ' » — préparation d’une copie de lecture…');
    fetch('/api/proxies/force', { method: 'POST' })
      .catch(function () { status('Copie de lecture impossible.'); });
  }

  /* Reprendre un média choisi par erreur, ou simplement dessiner sur le fond
   * seul : sans cette sortie, le seul recours était de relancer la session. */
  $('tp-clear').addEventListener('click', function () {
    if (recording || previewing) {
      status('Impossible de retirer le média pendant l’enregistrement ou la prévisualisation.');
      return;
    }
    // Retirer le média ici ne le retirerait que de cet écran : le poste
    // continuerait de l'afficher et la tablette dessinerait sans référence.
    if (isTablet) {
      status('Le retrait du rush se fait depuis le poste.');
      return;
    }
    if (!mediaNode) return;
    releaseMedia();
    mediaType = null;
    mediaName = '';
    MediaTransport.detach();
    status('Média retiré : le canevas retrouve son fond.');
  });

  /* Le transcodage est arrêté par le serveur, pas par cet écran : c'est lui
   * qui tient le processus FFmpeg. Le voile disparaîtra à l'état publié en
   * retour, comme pour n'importe quel changement de média. */
  $('media-prep-cancel').addEventListener('click', function () {
    if (isTablet) return;
    $('media-prep-cancel').disabled = true;
    fetch('/api/media/cancel', { method: 'POST' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        status(data.cancelled
          ? 'Préparation annulée : le rush n’a pas été installé.'
          : 'La préparation était déjà terminée.');
      })
      .catch(function () { status('Impossible de joindre le serveur.'); })
      .then(function () { $('media-prep-cancel').disabled = false; });
  });

  /* Baisser l'opacité fond désormais le média vers la couleur de fond choisie,
   * et non plus vers le blanc : c'est bien ce qu'on veut voir, puisque c'est
   * cette couleur qui sert de papier au tracé. */
  /* Changer de média en cours de route : le tracé déjà posé est conservé. */
  $('tp-choose').addEventListener('click', function () {
    if (recording || previewing) {
      status('Impossible de changer de média pendant l’enregistrement ou la prévisualisation.');
      return;
    }
    if (isTablet) {
      status('Le choix du rush se fait depuis le poste.');
      return;
    }
    openMediaOnServer('media-input');
  });

  $('media-input').addEventListener('change', function (event) {
    loadMedia(event.target.files[0]);
    event.target.value = '';   // permet de recharger deux fois le même fichier
  });

  $('setup-media-browse').addEventListener('click', function () {
    if (isTablet) return;
    openMediaOnServer('setup-media');
  });

  $('setup-media').addEventListener('change', function (event) {
    var file = event.target.files[0];
    if (!file) return;
    $('setup-media-name').value = file.name;
    loadMedia(file);
  });

  /* ------------------------------------------- Ajustement du média */

  /* Tout est exprimé en pourcentages du cadre, jamais en `transform` : une
   * couche promue par `transform` est composée dans un autre chemin
   * colorimétrique et l'image y perd visiblement de la saturation (voir
   * dev/brief.md). Des pourcentages suivent en prime le zoom de la vue et le
   * redimensionnement de la fenêtre sans qu'on ait rien à recalculer. */

  var SAME_FORMAT = 0.005;   // tolérance d'égalité des ratios

  /** Écart de format entre le média et le canevas. 1 = formats identiques. */
  function mediaRatio() {
    if (!mediaSize.width || !mediaSize.height) return 1;
    return (mediaSize.width / mediaSize.height) / (config.width / config.height);
  }

  /**
   * Part du média que le cadre du canevas couvre au zoom 1, en fractions du
   * média (0..1). C'est la plus grande zone au format du canevas qui tienne
   * dedans : elle touche les deux bords de l'axe long, et laisse du jeu sur
   * l'autre — le jeu dans lequel on fait glisser le cadre.
   */
  function coverFractions() {
    var r = mediaRatio();
    return r >= 1 ? { w: 1 / r, h: 1 } : { w: 1, h: r };
  }

  /**
   * Région du média retenue par le recadrage, en fractions du média.
   *
   * Zoomer resserre la région (elle est divisée par le zoom) sans jamais la
   * faire sortir du média : `posX`/`posY` la promènent sur tout le jeu restant,
   * bornes comprises. Aucun réglage ne peut donc rendre un bord inatteignable.
   */
  function cropRect(fit) {
    var cover = coverFractions();
    var w = cover.w / fit.zoom;
    var h = cover.h / fit.zoom;
    return { x: (1 - w) * fit.posX, y: (1 - h) * fit.posY, w: w, h: h };
  }

  /**
   * Géométrie du média dans le cadre du canevas, en pourcentages de ce cadre.
   *
   * `contain` fait entrer le média en entier : un média vertical n'est pas
   * rogné en haut et en bas, un horizontal ne l'est pas sur les côtés, et le
   * fond du canevas apparaît dans l'espace restant.
   *
   * `crop` fait l'inverse : la région retenue par `cropRect()` occupe tout le
   * cadre, donc le média entier y est agrandi de `1 / rect.w` et décalé de
   * `-rect.x` fois cette largeur. Comme la région tient toujours dans le média,
   * le média couvre toujours le cadre sans trou.
   *
   * Formats identiques (r = 1) au zoom 1 : les deux modes donnent 100/100/0/0,
   * soit exactement le plein cadre. Le filet clair que laissait un ajustement
   * au demi-pixel près ne peut donc pas réapparaître.
   */
  function fitBox(fit) {
    if (fit.mode === 'crop') {
      var rect = cropRect(fit);
      return {
        w: 100 / rect.w, h: 100 / rect.h,
        left: -100 * rect.x / rect.w, top: -100 * rect.y / rect.h
      };
    }
    var r = mediaRatio();
    var w = r >= 1 ? 100 : 100 * r;
    var h = r >= 1 ? 100 / r : 100;
    return { w: w, h: h, left: (100 - w) / 2, top: (100 - h) / 2 };
  }

  function writeBox(node, box) {
    node.style.width = box.w + '%';
    node.style.height = box.h + '%';
    node.style.left = box.left + '%';
    node.style.top = box.top + '%';
  }

  function applyMediaFit() {
    if (!mediaNode || mediaType === 'audio' || !mediaSize.width) return;
    writeBox(mediaNode, fitBox(mediaFit));
  }

  /** Métadonnées arrivées : on connaît enfin les dimensions du média. */
  function onMediaReady() {
    if (!mediaNode || mediaType === 'audio') return;
    var width = mediaNode.videoWidth || mediaNode.naturalWidth;
    var height = mediaNode.videoHeight || mediaNode.naturalHeight;
    if (!width || !height) return;

    setMediaLoading(false);
    mediaSize = { width: width, height: height };
    // La tablette reçoit la configuration avant le média : le cadrage arrivé
    // là a été effacé par le `releaseMedia()` de l'installation du rush. On le
    // repose ici, une fois les dimensions connues.
    if (isTablet && sessionFit) mediaFit = cloneFit(sessionFit);
    applyMediaFit();
    $('tp-fit').style.display = '';

    // Même raison que le cadrage : les bornes reçues avant le média ont été
    // écrêtées contre une durée encore inconnue. On les repose maintenant.
    if (isTablet && sessionRange && MediaTransport.isTimed()) {
      MediaTransport.setRange(sessionRange.in, sessionRange.out);
    }

    if (isTablet) return;
    // Tant que l'espace n'existe pas, la question du cadrage n'a pas de sens :
    // `createWorkspace` rappellera une fois le format réel connu, et c'est ce
    // passage-là qui doit compter comme la question posée.
    if (!ready) return;

    // Un projet est en train de s'ouvrir : son cadrage est écrit dans le
    // fichier, la question a déjà sa réponse. Le média qui est encore là est
    // celui d'avant — on ne lui demande rien, il va être remplacé.
    if (pendingProject) { answeredFitQuestion = mediaFitQuestion(); return; }

    // Formats divergents : plutôt que d'imposer un letterboxing, on demande.
    // Une fois par image, et pas une fois par chargement : armer le mode
    // tablette prépare un proxy de lecture, ce qui republie le même rush sous
    // un nouvel identifiant. La fenêtre se rouvrait alors sur un cadrage déjà
    // décidé, qu'elle proposait de refaire. Le cadrage en cours fait foi.
    var question = mediaFitQuestion();
    if (question !== null && question === answeredFitQuestion) return;
    answeredFitQuestion = question;
    if (Math.abs(mediaRatio() - 1) >= SAME_FORMAT) openCrop();
  }

  /* Ce qui identifie la *question* de cadrage, et non le média servi : deux
   * préparations de la même source la posent à l'identique. Trois choses la
   * changent vraiment — l'image, la page du PDF (un autre format possible), et
   * le format du canevas, puisque la question est « comment cette image tient
   * dans ce cadre ». Refaire l'espace de travail la repose donc. */
  var answeredFitQuestion = null;

  function mediaFitQuestion() {
    if (!currentMedia) return null;
    var source = currentMedia.sourcePath || currentMedia.name;
    if (!source) return null;
    return source + '#' + (currentMedia.page || 0)
      + '@' + config.width + 'x' + config.height;
  }

  /* ------------------------------------------- Fenêtre de cadrage */

  var cropDraft = null;      // état en cours d'édition, appliqué seulement à la validation
  var cropNode = null;       // aperçu du média, distinct de celui du canevas
  var cropBox = { width: 0, height: 0 };   // le média à l'écran, au zoom 1
  var cropDrag = null;

  function openCrop() {
    if (!mediaNode || mediaType === 'audio' || !mediaSize.width) return;
    // Tant que l'espace n'existe pas, le format du canevas est celui par
    // défaut : la question n'aurait pas de sens. `createWorkspace` rappelle
    // `onMediaReady` une fois le format réel connu.
    if (!ready) return;
    // Le cadrage se décide sur le poste, comme le choix du rush : un seul
    // arbitre, sinon les deux écrans se contrediraient. La tablette reçoit le
    // résultat par `mediaFit` dans la configuration publiée.
    if (isTablet) return;
    cropDraft = { mode: mediaFit.mode, zoom: mediaFit.zoom, posX: mediaFit.posX, posY: mediaFit.posY };

    $('crop-hint').textContent = Math.abs(mediaRatio() - 1) < SAME_FORMAT
      ? 'Le média (' + mediaSize.width + '×' + mediaSize.height + ') est au format du canevas.'
      : 'Le média (' + mediaSize.width + '×' + mediaSize.height + ') n’a pas le format du '
        + 'canevas (' + config.width + '×' + config.height + '). Choisissez comment le placer.';

    buildCropNode();
    $('crop-mode').value = cropDraft.mode;
    $('crop-zoom').value = Math.round(cropDraft.zoom * 100);
    syncCropControls();
    $('crop-screen').style.display = 'flex';
  }

  function closeCrop() {
    $('crop-screen').style.display = 'none';
    // On relâche le décodeur, mais surtout pas l'URL objet : elle est partagée
    // avec le média vivant du canevas.
    if (cropNode) {
      if (cropNode.tagName === 'VIDEO') cropNode.pause();
      cropNode.removeAttribute('src');
      cropNode.remove();
      cropNode = null;
    }
    cropDrag = null;
  }

  /** L'aperçu du média, posé sous le cadre. */
  function buildCropNode() {
    // Un élément distinct partageant la même URL objet : déplacer le média du
    // canevas dans la modale casserait le transport et le tracé en cours.
    if (mediaType === 'video') {
      cropNode = document.createElement('video');
      cropNode.muted = true;
      cropNode.playsInline = true;
      cropNode.addEventListener('loadedmetadata', function () {
        // Cadrer sur l'image que l'utilisateur a sous les yeux, pas sur la première.
        try { cropNode.currentTime = mediaNode.currentTime; } catch (e) { /* pas encore seekable */ }
      });
    } else {
      cropNode = document.createElement('img');
    }
    cropNode.draggable = false;
    cropNode.src = mediaUrl;
    // Avant le cadre, sinon le média se peindrait par-dessus : à position égale,
    // c'est le dernier élément du flux qui l'emporte.
    $('crop-stage').insertBefore(cropNode, $('crop-frame'));
  }

  /**
   * Dimensionne la scène. En recadrage elle vaut le média à son format
   * d'origine — c'est lui qu'on doit voir en entier pour juger de ce qu'on
   * écarte. En « ratio d'origine » elle vaut le canevas, le média y entrant en
   * entier : la scène est alors l'aperçu exact du résultat.
   */
  /**
   * Largeur réellement offerte à la scène par la modale, bordures et marges
   * intérieures déduites.
   *
   * Une largeur en dur ne pouvait pas coller : la modale « large » fait 560 px
   * bordures comprises (`box-sizing: border-box`) moins 2×24 px de marge
   * intérieure, soit 510 px — et la scène en réclamait 512. Deux pixels de
   * trop, mais `overflow-y: auto` sur la modale rend l'axe horizontal
   * scrollable dès qu'il déborde : le bord droit du cadre sortait du champ.
   */
  /* Bordure de #crop-stage, haut + bas et gauche + droite (1 px de chaque
   * côté). En dur plutôt que mesurée : la scène est dimensionnée avant d'être
   * affichée, et `getComputedStyle` sur un élément encore masqué ne rend rien
   * d'exploitable. Doit suivre la règle CSS. */
  var CROP_STAGE_BORDER = 2;

  function cropStageRoom() {
    var host = $('crop-stage').parentElement;
    if (!host) return 512;
    var style = window.getComputedStyle(host);
    var room = host.clientWidth
      - parseFloat(style.paddingLeft || 0)
      - parseFloat(style.paddingRight || 0);
    return room > 160 ? Math.floor(room) : 512;
  }

  function layoutCropStage() {
    // La scène prend toujours le ratio du canevas : en recadrage elle *est* le
    // cadre (l'image glisse dedans, l'`overflow: hidden` rogne ce qui dépasse) ;
    // en « ratio d'origine » c'est l'aperçu exact du résultat.
    var ratio = config.width / config.height;

    // Sans borne en hauteur, un format vertical (1080×1920) donnerait une scène
    // de 900 px de haut qui déborderait de la modale.
    var maxWidth = Math.min(512, cropStageRoom() - CROP_STAGE_BORDER);
    var maxHeight = Math.max(240, Math.round(window.innerHeight * 0.5));
    if (maxWidth / ratio <= maxHeight) {
      cropBox = { width: maxWidth, height: Math.round(maxWidth / ratio) };
    } else {
      cropBox = { width: Math.round(maxHeight * ratio), height: maxHeight };
    }
    // `cropBox` décrit la boîte de *contenu* — c'est en elle que se placent le
    // média et le cadre, tous deux en position absolue. La scène, elle, est en
    // `box-sizing: border-box` : la largeur qu'on lui pose inclut ses bordures.
    // Les poser à l'identique retranchait donc 2 px au contenu, et le bord
    // droit du cadre — large de 1 px, calé à l'extrémité — tombait sous
    // l'`overflow: hidden`. Visible nulle part, alors que les trois autres
    // côtés s'affichaient.
    $('crop-stage').style.width = (cropBox.width + CROP_STAGE_BORDER) + 'px';
    $('crop-stage').style.height = (cropBox.height + CROP_STAGE_BORDER) + 'px';
  }

  /**
   * Place le média dans la scène.
   *
   * En mode recadrage la scène *est* le cadre (ratio du canevas). L'image est
   * mise à l'échelle pour que la région retenue (`rect`) couvre exactement la
   * scène ; le zoom agrandit l'image au-delà de ses bords, que l'`overflow:
   * hidden` de la scène rogne. Glisser déplace l'image, le cadre reste fixe.
   *
   * En mode « ratio d'origine » l'image est simplement ajustée pour tenir
   * entière dans la scène canvas.
   */
  function paintCropStage() {
    if (!cropNode) return;
    var crop = cropDraft.mode === 'crop';
    // En mode recadrage la scène est le cadre : crop-frame n'apporte rien.
    $('crop-frame').style.display = 'none';
    if (!crop) { writeBox(cropNode, fitBox(cropDraft)); return; }

    var cover = coverFractions();
    var rect = cropRect(cropDraft);
    var zoom = cropDraft.zoom;
    // L'image est mise à l'échelle pour que cover (= un canevas entier) remplisse
    // la scène à zoom 1. Le zoom agrandit proportionnellement au-delà.
    var width  = cropBox.width  * zoom / cover.w;
    var height = cropBox.height * zoom / cover.h;
    // On décale l'image pour amener la région retenue à l'origine de la scène.
    var left = -rect.x * width;
    var top  = -rect.y * height;

    cropNode.style.width  = width  + 'px';
    cropNode.style.height = height + 'px';
    cropNode.style.left   = left   + 'px';
    cropNode.style.top    = top    + 'px';
  }

  /** Jeu de l'image sur chaque axe, en pixels de la scène. 0 = axe figé. */
  function cropSlack() {
    var cover = coverFractions();
    var rect = cropRect(cropDraft);
    // image_width - stage_width = cropBox.width*zoom/cover.w - cropBox.width
    //   = cropBox.width * (zoom/cover.w - 1) = (1 - rect.w) * cropBox.width * zoom / cover.w
    return {
      x: (1 - rect.w) * cropBox.width  * cropDraft.zoom / cover.w,
      y: (1 - rect.h) * cropBox.height * cropDraft.zoom / cover.h
    };
  }

  function syncCropControls() {
    var crop = cropDraft.mode === 'crop';
    $('crop-zoom-group').style.display = crop ? '' : 'none';
    $('crop-zoom-value').textContent = Math.round(cropDraft.zoom * 100) + ' %';
    layoutCropStage();
    paintCropStage();
    var slack = crop ? cropSlack() : { x: 0, y: 0 };
    $('crop-stage').classList.toggle('locked', slack.x < 1 && slack.y < 1);
  }

  $('crop-mode').addEventListener('change', function () {
    cropDraft.mode = $('crop-mode').value;
    // Repartir d'un cadrage neutre : garder le zoom d'un aller-retour donnerait
    // un recadrage arbitraire au retour dans le mode.
    if (cropDraft.mode === 'contain') {
      cropDraft.zoom = 1;
      cropDraft.posX = 0.5;
      cropDraft.posY = 0.5;
      $('crop-zoom').value = 100;
    }
    syncCropControls();
  });

  $('crop-zoom').addEventListener('input', function (event) {
    cropDraft.zoom = Math.max(1, parseInt(event.target.value, 10) / 100);
    syncCropControls();
  });

  /* Glisser : c'est le cadre qu'on déplace sur le média, il doit donc suivre le
   * pointeur — un déplacement écran se convertit en variation de position à la
   * mesure du jeu réellement disponible sur chaque axe. Un axe sans jeu (le
   * cadre y touche déjà les deux bords) ne bouge pas. */
  $('crop-stage').addEventListener('pointerdown', function (event) {
    if (!cropDraft || cropDraft.mode !== 'crop') return;
    cropDrag = { x: event.clientX, y: event.clientY };
    $('crop-stage').classList.add('dragging');
    $('crop-stage').setPointerCapture(event.pointerId);
  });

  $('crop-stage').addEventListener('pointermove', function (event) {
    if (!cropDrag) return;
    var slack = cropSlack();
    // On saisit le média, on ne promène pas une fenêtre au-dessus de lui :
    // tirer vers la gauche emmène l'image à gauche, donc découvre sa droite.
    // `posX` étant la position du *cadre* dans le média, il va donc à l'inverse
    // du doigt. L'ancien sens donnait le geste contraire de tous les outils qui
    // manipulent une image.
    if (slack.x >= 1) cropDraft.posX = clamp(cropDraft.posX - (event.clientX - cropDrag.x) / slack.x, 0, 1);
    if (slack.y >= 1) cropDraft.posY = clamp(cropDraft.posY - (event.clientY - cropDrag.y) / slack.y, 0, 1);
    cropDrag = { x: event.clientX, y: event.clientY };
    syncCropControls();
  });

  ['pointerup', 'pointercancel'].forEach(function (type) {
    $('crop-stage').addEventListener(type, function (event) {
      cropDrag = null;
      $('crop-stage').classList.remove('dragging');
      try { $('crop-stage').releasePointerCapture(event.pointerId); } catch (e) { /* déjà relâché */ }
    });
  });

  $('crop-stage').addEventListener('wheel', function (event) {
    if (!cropDraft || cropDraft.mode !== 'crop') return;
    event.preventDefault();
    cropDraft.zoom = clamp(cropDraft.zoom * (event.deltaY < 0 ? 1.08 : 1 / 1.08), 1, 4);
    $('crop-zoom').value = Math.round(cropDraft.zoom * 100);
    syncCropControls();
  }, { passive: false });

  $('crop-cancel').addEventListener('click', closeCrop);

  $('crop-apply').addEventListener('click', function () {
    mediaFit = cropDraft;
    applyMediaFit();
    publishConfig();
    closeCrop();
    status(mediaFit.mode === 'crop'
      ? 'Média recadré au format du canevas.'
      : 'Média conservé dans son ratio d’origine.');
  });

  $('tp-fit').addEventListener('click', function () {
    if (recording || previewing) {
      status('Impossible de recadrer pendant l’enregistrement ou la prévisualisation.');
      return;
    }
    // Même règle que le choix du rush : le poste décide, sans quoi les deux
    // écrans cadreraient le même média différemment.
    if (isTablet) {
      status('Le cadrage du média se fait depuis le poste.');
      return;
    }
    openCrop();
  });

  function inPoint() {
    return MediaTransport.isTimed() ? MediaTransport.inPoint() : 0;
  }

  function outPoint() {
    return MediaTransport.isTimed() ? MediaTransport.outPoint() : Infinity;
  }

  /** Taille de l'image ajustée au cadre — la référence du zoom 1. */
  function resizeWorkspace() {
    var workspace = $('workspace');
    // Les commandes de zoom et les curseurs de défilement flottent au-dessus
    // de l'image : ils ne retirent rien à la place disponible.
    var availableWidth = workspace.clientWidth;
    var availableHeight = workspace.clientHeight;
    var ratio = config.width / config.height;

    if (availableWidth / availableHeight > ratio) {
      fit.height = availableHeight;
      fit.width = availableHeight * ratio;
    } else {
      fit.width = availableWidth;
      fit.height = availableWidth / ratio;
    }
    applyView();
  }

  /* ----------------------------------------------------- Enregistrement */

  function countdown(callback) {
    var node = $('countdown');
    node.style.display = 'flex';
    var value = 3;
    node.textContent = value;
    var timer = setInterval(function () {
      value -= 1;
      if (value > 0) {
        node.textContent = value;
      } else {
        clearInterval(timer);
        node.style.display = 'none';
        callback();
      }
    }, 1000);
  }

  $('btn-rec').addEventListener('click', function () {
    if (!ready || previewing) return;
    // Un seul enregistrement à la fois, tenu par celui qui dessine : deux
    // décomptes lancés en parallèle donneraient deux origines de temps
    // différentes pour un même tracé.
    if (viewing) { status(handOverNotice('enregistrer')); return; }
    // Le rush se cale sur son point IN pendant le décompte : le décodeur a
    // trois secondes pour préparer la première image, et il ne reste plus à
    // l'instant du départ que le temps de la présenter.
    seekToInPoint();
    countdown(function () {
      clearActiveLayer();
      startUnderlay();
      recording = true;
      // `t0` reste ouvert : c'est la première image du rush qui le posera.
      // Un point tracé avant elle le poserait lui-même (voir `elapsed`), ce
      // qui reste préférable à un tracé sans horodatage.
      t0 = null;
      lastElapsed = 0;
      recordStart = performance.now();

      $('btn-rec').style.display = 'none';
      $('btn-stop').style.display = 'block';
      document.body.classList.add('recording');
      status('Enregistrement en cours…');
      emit({ t: 'rec.start' });

      if (mediaNode && mediaType !== 'image') {
        MediaTransport.setLocked(true);
        watchRecordedRange();
      }

      playFromInPoint(function (lag) {
        // La première image du rush est à l'écran : c'est l'origine des temps
        // du tracé, reculée du retard qu'elle avait déjà pris sur le point IN.
        // Sauf si un point est déjà tombé — déplacer l'origine après coup
        // décalerait ce qui a été enregistré.
        if (t0 === null) t0 = performance.now() - lag;
        recordStart = t0;
      });
      if (replay) recFrame = requestAnimationFrame(recTick);
    });
  });

  $('btn-stop').addEventListener('click', stopRecording);

  /* Surveillance du point OUT pendant l'enregistrement, calée sur l'image.
   *
   * `timeupdate` ne se déclenche que quatre à cinq fois par seconde : le rush
   * dépassait le point OUT de plusieurs images avant qu'on ne s'en aperçoive,
   * et celle qui restait figée à l'écran était hors de la plage annotée. Sur
   * tablette, où le rush arrive par le réseau et où le décodeur avance par
   * à-coups, c'était le plus visible.
   *
   * On lit donc la tête de lecture à chaque rafraîchissement d'affichage.
   * `timeupdate` reste branché en second filet : un onglet en arrière-plan
   * n'anime plus rien, et l'enregistrement doit finir quand même.
   *
   * Le même passage recale l'horloge du tracé sur celle du rush — c'est le
   * seul endroit où l'on tient les deux en main en même temps. */
  function watchRecordedRange() {
    // Un média dont personne ne sait dire la durée n'a pas de borne à
    // surveiller — le recalage de l'horloge, lui, reste utile.
    var bounded = isFinite(outPoint());

    function reached() {
      return bounded && MediaTransport.reachedOut(mediaNode.currentTime || 0);
    }

    function check() {
      if (!recording || !mediaNode || mediaType === 'image') return;
      syncClockToMedia(mediaNode.currentTime || 0);
      if (reached()) { stopRecording(); return; }
      requestAnimationFrame(check);
    }

    // Le guet décisif : l'image présentée, pas l'heure qu'il est. Voir
    // `watchOut` dans transport.js — c'est ce qui empêche l'image d'après le
    // point OUT de passer à l'écran avant qu'on ait pu arrêter le lecteur.
    stopWatchingOut = bounded ? MediaTransport.watchOut(stopRecording) : null;

    playbackListener = function () {
      if (recording && mediaNode && reached()) stopRecording();
    };
    mediaNode.addEventListener('timeupdate', playbackListener);
    requestAnimationFrame(check);
  }

  /** Portion de rush réellement parcourue, en millisecondes, ou `null` quand
   *  le média n'a pas d'horloge (une image de fond).
   *
   *  Elle se compte en **images entières** : de celle du point IN à celle qui
   *  est à l'écran, incluse. Mesurer l'écart brut entre deux instants rendait
   *  ce compte dépendant de l'endroit exact où le lecteur s'était arrêté —
   *  arrêté sur le *début* de la dernière image, il en manquait une, et la
   *  couche « média » de l'export était une image plus courte que la plage
   *  annotée. */
  function mediaSpanElapsed() {
    if (!mediaNode || mediaType === 'image' || !MediaTransport.isTimed()) return null;
    var start = inPoint();
    var end = outPoint();
    var step = MediaTransport.frameStep();
    var at = clamp(mediaNode.currentTime || 0, start,
                   isFinite(end) ? end - step / 2 : Infinity);
    var shown = MediaTransport.frameOf(at) - MediaTransport.frameOf(start) + 1;
    return Math.max(1, shown) * step * 1000;
  }

  /* Les boutons de la barre portent une icône SVG et une légende : écraser
   * leur `textContent` emporterait l'icône avec le texte. On ne touche donc
   * qu'à la légende, et l'icône reste celle du gabarit. */
  function setToolLabel(id, text) {
    var label = $(id).querySelector('span');
    if (label) label.textContent = text;
  }

  /** Bascule l'icône « lecture » / « arrêt » du bouton de prévisualisation. */
  function setPreviewIcon(playing) {
    var shape = $('btn-preview').querySelector('svg .filled');
    if (!shape) return;
    shape.setAttribute('d', playing
      ? 'M7 7h10v10H7z'                 // carré : arrêter
      : 'M8 5.5v13l10-6.5z');           // triangle : lire
  }

  function stopRecording() {
    if (!recording) return;
    recording = false;
    // Arrêter le lecteur d'abord, avant tout calcul et avant tout message :
    // chaque instruction qui passe avant est du temps pendant lequel le
    // compositeur peut présenter l'image suivante — celle qu'on ne veut
    // justement jamais montrer.
    if (mediaNode && playbackListener) mediaNode.pause();
    if (stopWatchingOut) { stopWatchingOut(); stopWatchingOut = null; }
    if (mediaNode && playbackListener) {
      mediaNode.removeEventListener('timeupdate', playbackListener);
      playbackListener = null;
    }

    // La durée enregistrée est celle de la portion de rush parcourue : c'est
    // elle qui donne le nombre d'images à l'export. L'horloge du système
    // servait de mesure et pouvait avoir dérivé de quelques millisecondes sur
    // celle du décodeur — assez pour une image de trop en bout de couche.
    var span = mediaSpanElapsed();
    recordedDuration = span === null ? performance.now() - recordStart : span;
    // Chaque couche garde la durée de SA prise : la durée du rendu est le
    // maximum, une prise courte par-dessus une longue ne raccourcit rien.
    if (activeLayer()) activeLayer().durationMs = recordedDuration;
    stopUnderlay();
    emit({ t: 'rec.stop', duration: recordedDuration });

    if (MediaTransport.isTimed()) {
      MediaTransport.setLocked(false);
      MediaTransport.parkAtOut();
    }
    document.body.classList.remove('recording');
    $('btn-stop').style.display = 'none';
    $('btn-rec').style.display = 'block';
    setToolLabel('btn-rec', 'Refaire');
    updateExportState();
  }

  $('btn-clear').addEventListener('click', function () {
    if (viewing) { status(handOverNotice('effacer')); return; }
    if (recording) {
      // Pendant le REC on repart d'une toile vierge sans perdre l'enregistrement :
      // l'effacement devient un évènement de la timeline, rejoué à l'export.
      var at = elapsed();
      applyClear(activeLayer(), at);
      emit({ t: 'clear', at: at });
      status('Couche effacée — le tracé déjà enregistré est conservé.');
      return;
    }
    stopPreview();
    clearActiveLayer();
    emit({ t: 'reset' });
    status(layers.length > 1
      ? activeLayer().name + ' effacée — les couches verrouillées sont intactes.'
      : 'Zone de dessin effacée.');
  });

  /* --------------------------------------------------- Prévisualisation */

  /* Rejeu du tracé enregistré, synchronisé avec le média de fond. La logique
   * est celle du renderer Python : chaque trace est rasterisée dans son propre
   * calque, puis fusionnée dans le canvas une fois terminée. */

  /** Rasterisation d'UN tracé dans son propre tampon — du posé du stylet au
   *  lâché. À ne pas confondre avec une couche, qui en contient plusieurs.
   *
   *  `seed` est recopiée : le tampon sert aussi de mémoire au tracé le temps
   *  qu'il s'écrive, et c'est à partir de lui que le poste reconstitue ce que
   *  la tablette a dessiné (voir `remoteStrokeEnd`). Sans elle, ce tracé-là
   *  repartait à l'export sur une autre graine que celle affichée sous le
   *  stylet — donc une autre dispersion et d'autres taches. */
  function makeTrace(item, reuse) {
    // `reuse` : un même tampon sert à toute une reconstruction. Les traces y
    // passent l'une après l'autre, jamais en même temps — allouer un canevas
    // pleine définition par trace rendait une annulation quadratique, et
    // remonter deux cents gestes allouait deux cents fois huit mégaoctets.
    var traceCanvas = reuse || document.createElement('canvas');
    if (traceCanvas.width !== canvas.width || traceCanvas.height !== canvas.height) {
      traceCanvas.width = canvas.width;
      traceCanvas.height = canvas.height;
    } else if (reuse) {
      traceCanvas.getContext('2d').clearRect(0, 0, traceCanvas.width, traceCanvas.height);
    }
    var traceCtx = traceCanvas.getContext('2d');
    return {
      canvas: traceCanvas,
      ctx: traceCtx,
      brush: item.brush,
      seed: item.seed,
      stamper: new BE.StrokeStamper(item.brush, item.seed, traceCtx),
      points: item.points,
      cursor: 0
    };
  }

  /** Reconstruit l'état final du dessin (traces + effacements) sans animation. */
  function rebuildFinalCanvas() {
    layers.forEach(rebuildLayer);
  }

  /** Variante asynchrone : reconstruction par tranches pour rester réactif.
   *  `onProgress(ratio)` est appelé après chaque tranche (0..1) ; une valeur
   *  égale à 1 signale la fin. */
  function rebuildFinalCanvasAsync(onProgress) {
    // Le travail est aplati couche par couche : la barre de progression compte
    // des traces, pas des couches — trois couches très inégales feraient
    // sinon une barre qui saute.
    var work = [];
    layers.forEach(function (layer) {
      layer.ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
      var lastClear = layer.clears.length ? Math.max.apply(null, layer.clears) : -Infinity;
      layer.strokes.forEach(function (item) {
        work.push({ layer: layer, item: item, from: lastClear });
      });
    });

    var i = 0;
    var total = work.length;
    if (total === 0) { onProgress(1); return; }

    function chunk() {
      // Budjet de 50 ms par tranche : l'interface reste responsive sans être trop lente.
      var deadline = performance.now() + 50;
      while (i < total && performance.now() < deadline) {
        var job = work[i++];
        var points = job.item.points.filter(function (p) { return p.t >= job.from; });
        if (points.length) {
          var trace = makeTrace({ brush: job.item.brush, seed: job.item.seed, points: points });
          feedTrace(trace, Infinity);
          commitTrace(trace, job.layer.ctx);
        }
      }
      onProgress(i / total);
      if (i < total) requestAnimationFrame(chunk);
    }
    requestAnimationFrame(chunk);
  }

  /** Alimente un tracé jusqu'à l'instant donné. Retourne true s'il est fini. */
  function feedTrace(trace, until) {
    while (trace.cursor < trace.points.length && trace.points[trace.cursor].t <= until) {
      var point = trace.points[trace.cursor];
      if (trace.cursor === 0) trace.stamper.begin(point); else trace.stamper.extend(point);
      trace.cursor += 1;
    }
    return trace.cursor >= trace.points.length;
  }

  /** Fusionne un tracé terminé dans le canevas d'une couche. */
  function commitTrace(trace, target) {
    composite(target, trace.canvas, trace.brush);
  }

  function startPreview() {
    if (previewing || drawing || recording || !visibleStrokeCount()) return;

    previewing = true;
    previewDuration = duration() + 400;
    // Toutes les couches se rejouent, chacune dans son canevas : l'ordre
    // d'empilement tient tout seul au moment de composer (voir `redraw`).
    replay = makeReplay(layers);
    layers.forEach(function (layer) {
      layer.ctx.clearRect(0, 0, layer.canvas.width, layer.canvas.height);
    });
    redraw();

    setToolLabel('btn-preview', 'Arrêter');
    setPreviewIcon(true);
    $('btn-export').disabled = true;
    $('btn-rec').disabled = true;
    hideCursor();

    // Même ancrage qu'à l'enregistrement : le rejeu doit partir de l'image que
    // le rush affiche réellement, pas de l'instant où on lui a demandé de
    // jouer. Sans cela la prévisualisation ne montrait pas la même
    // synchronisation que l'export.
    previewStart = performance.now();
    playFromInPoint(function () { previewStart = performance.now(); });
    previewFrame = requestAnimationFrame(previewTick);
  }

  function previewTick() {
    var elapsedMs = performance.now() - previewStart;

    feedReplay(replay, elapsedMs);
    redraw();
    status('Prévisualisation — ' + (elapsedMs / 1000).toFixed(1)
      + ' s / ' + (previewDuration / 1000).toFixed(1) + ' s');

    if (elapsedMs >= previewDuration && replayFinished(replay)) {
      stopPreview();
      return;
    }
    previewFrame = requestAnimationFrame(previewTick);
  }

  function stopPreview() {
    if (!previewing) return;
    previewing = false;
    if (previewFrame) cancelAnimationFrame(previewFrame);
    previewFrame = null;

    // Termine instantanément le rejeu pour retrouver l'état final du dessin.
    replay = null;
    rebuildFinalCanvas();

    if (mediaNode && mediaType !== 'image') mediaNode.pause();

    setToolLabel('btn-preview', 'Préviz');
    setPreviewIcon(false);
    $('btn-rec').disabled = false;
    redraw();
    updateExportState();
  }

  $('btn-preview').addEventListener('click', function () {
    if (previewing) stopPreview(); else startPreview();
  });

  /* ----------------------------------------------------------- Export */

  function pad2(value) { return (value < 10 ? '0' : '') + value; }

  /** Nom de fichier sans son extension — « rush.mov » → « rush ».
   *  On ne retire qu'un suffixe court : « doc.pdf (page 3) » garde sa page. */
  function withoutExtension(name) {
    return (name || '').replace(/\.[A-Za-z0-9]{1,5}$/, '');
  }

  /** « [date]_[heure] », pour distinguer deux rendus du même rush. */
  function timeStamp() {
    var now = new Date();
    return now.getFullYear() + pad2(now.getMonth() + 1) + pad2(now.getDate())
      + '_' + pad2(now.getHours()) + pad2(now.getMinutes()) + pad2(now.getSeconds());
  }

  /** « [média]_livenote_[date][heure] » — modifiable par l'utilisateur. */
  function defaultExportName() {
    var base = mediaName ? withoutExtension(mediaName) + '_livenote_' : 'livenote_';
    return base + timeStamp();
  }

  /** Le dossier d'un export pro porte le nom du média et l'horodatage : le
   *  premier dit de quel rush on parle, le second distingue deux essais du
   *  même — et les trois fichiers, qui en héritent, restent identifiables une
   *  fois sortis du dossier et jetés dans un chutier. */
  function defaultFolderName() {
    var media = exportableMedia();
    var base = withoutExtension((media && media.name) || mediaName) || 'livenote';
    return base + '_' + timeStamp();
  }

  /**
   * Le média tel que l'export peut le rendre, ou null.
   *
   * Condition unique : le serveur doit savoir où le fichier se trouve. Un média
   * ouvert par le sélecteur du navigateur n'est qu'un blob anonyme — il se
   * regarde à l'écran, il ne se réencode pas.
   *
   * Un son en fait partie : il n'a rien à montrer sous le tracé, mais sa bande
   * son est une couche à part entière, coupée aux mêmes bornes.
   */
  function exportableMedia() {
    if (!currentMedia || !mediaNode) return null;
    if (!currentMedia.sourcePath && !currentMedia.servedPath) return null;
    if (['video', 'image', 'audio'].indexOf(currentMedia.kind) < 0) return null;
    return currentMedia;
  }

  /** Ce média a-t-il quelque chose à montrer sous le tracé ? */
  function visualMedia() {
    var media = exportableMedia();
    return media && media.kind !== 'audio' ? media : null;
  }

  /* Dernier nom proposé par l'outil : sert à savoir si l'utilisateur.ice a
   * saisi le sien, auquel cas changer de mode ne doit pas l'écraser. */
  var lastProposedName = '';

  var EXPORT_HINTS = {
    preview: 'Une seule vidéo, sans alpha : le tracé aplati sur ce que montre le canevas.',
    prores: 'Une seule vidéo ProRes 4444 — avec couche alpha, ou aplatie.',
    pro: 'Trois fichiers dans un sous-dossier : l’aperçu aplati, le média cadré, '
      + 'le tracé en alpha. Même canevas, même cadence, même timecode de départ — '
      + 'les couches se réempilent au montage sans recalage.'
  };

  function syncExportDialog() {
    var hidden = layers.filter(function (layer) { return !layer.visible; }).length;
    var note = $('export-layers-note');
    note.style.display = hidden ? '' : 'none';
    note.textContent = hidden
      ? (layers.length - hidden) + ' couche(s) sur ' + layers.length + ' seront exportées — '
        + hidden + ' masquée(s).'
      : '';

    var mode = $('export-mode').value;
    var pro = mode === 'pro';
    var h264 = mode === 'preview';
    var media = exportableMedia();
    var visual = visualMedia();
    var flatten = $('export-flatten');

    // « Sur le média » n'existe que si le serveur peut le rendre ; l'alpha, que
    // si le fichier sait le porter. Un son ne passe pas sous le tracé : il
    // l'accompagne, et le fond reste uni.
    flatten.options[0].disabled = !media;
    flatten.options[0].textContent = visual || !media
      ? 'Le média, aplati sur l’arrière-plan'
      : 'Le son du média, sur le fond uni';
    flatten.options[2].disabled = h264;
    if (flatten.value === 'media' && !media) flatten.value = 'solid';
    if (flatten.value === 'alpha' && h264) flatten.value = media ? 'media' : 'solid';

    $('export-flatten-group').style.display = pro ? 'none' : '';
    // Rien n'entoure une couche son : la case n'a pas d'objet.
    $('export-media-alpha-row').style.display = pro && visual ? '' : 'none';
    $('export-ext').style.display = pro ? 'none' : '';
    $('export-ext').textContent = h264 ? '.mp4' : '.mov';
    $('export-name-label').textContent = pro ? 'Nom du dossier' : 'Nom du fichier';
    $('export-files').style.display = pro ? '' : 'none';
    if (pro) {
      var base = ($('export-name').value || defaultFolderName()).trim() || 'export';
      $('export-files').textContent = base + '_preview.mp4 · ' + base + '_media.'
        + (visual ? 'mov' : 'wav') + ' · ' + base + '_trace.mov';
    }
    $('export-mode-hint').textContent = (EXPORT_HINTS[mode] || '')
      + (media ? '' : ' Aucun média exportable : le tracé ne peut être aplati '
        + 'que sur le fond uni. (Un média ouvert depuis le navigateur plutôt que '
        + 'par « Parcourir… » reste inconnu du serveur.)')
      + (pro && media && !visual
        ? ' Le média est un son : la couche « média » est un WAV coupé aux mêmes '
          + 'bornes, à la définition de la source, et l’aperçu montre le tracé sur '
          + 'le fond uni.'
        : '');
  }

  function openExportDialog() {
    if (isTablet || !ready || !visibleStrokeCount() || recording || previewing || viewing) return;
    var media = exportableMedia();
    // L'export pro est fait de trois couches dont l'une est le média : sans
    // média — pas même un son — il n'y a rien à décomposer.
    $('export-mode').options[2].disabled = !media;
    if (!media && $('export-mode').value === 'pro') $('export-mode').value = 'prores';
    $('export-flatten').value = media ? 'media' : (config.alpha ? 'alpha' : 'solid');
    $('export-media-alpha').checked = false;
    $('export-width').value = config.width;
    $('export-height').value = config.height;
    $('export-fps').value = String(config.fps);
    $('export-name').value = $('export-mode').value === 'pro'
      ? defaultFolderName() : defaultExportName();
    lastProposedName = $('export-name').value;
    if (!$('export-dir').value) $('export-dir').value = exportDirectory;
    syncExportDialog();
    $('export-screen').style.display = 'flex';
  }

  $('btn-export').addEventListener('click', openExportDialog);

  function projectData() {
    return {
      format: 'live_notes',
      version: PROJECT_VERSION,
      savedAt: new Date().toISOString(),
      config: {
        width: config.width, height: config.height, fps: config.fps,
        alpha: config.alpha, background: config.background
      },
      layers: layers.map(function (layer) {
        return {
          id: layer.id,
          name: layer.name,
          visible: layer.visible,
          strokes: layer.strokes,
          clears: layer.clears,
          durationMs: layer.durationMs || 0
        };
      }),
      activeLayer: activeId,
      durationMs: duration(),
      // Points IN/OUT du transport, conservés pour restauration au rechargement.
      inPoint:  MediaTransport.isTimed() ? MediaTransport.inPoint()  : null,
      outPoint: MediaTransport.isTimed() ? MediaTransport.outPoint() : null,
      // De quoi retrouver le média tel qu'il était sous le tracé : le fichier
      // à son dernier emplacement connu, la page du PDF s'il y en a une, et le
      // cadrage décidé dans la fenêtre de recadrage. Sans eux, rouvrir un
      // projet laissait un dessin flottant au-dessus de rien.
      media: currentMedia ? {
        name: currentMedia.name,
        kind: currentMedia.kind,
        sourcePath: currentMedia.sourcePath || null,
        page: currentMedia.page || null,
        fit: cloneFit(mediaFit)
      } : null
    };
  }

  function saveProject() {
    if (isTablet || !ready) return;
    var name = (mediaName ? withoutExtension(mediaName) : 'live_notes') + '.lvn';
    status('Enregistrement du projet…');
    // Un `<a download>` sur un blob marche avec Chrome mais pas avec la
    // fenetre macOS (WKWebView navigue vers le blob et remplace l'ecran par
    // le JSON). Le nom et le dossier passent donc par un dialogue natif cote
    // serveur, comme pour le choix du media et du dossier d'export.
    fetch('/api/project/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: JSON.stringify(projectData(), null, 2),
        name: name
      })
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.error) { status(data.error); return; }
        status(data.cancelled ? 'Enregistrement annulé.' : 'Projet enregistré.');
      })
      .catch(function () { status('Impossible d’enregistrer le projet.'); });
  }

  function loadProject(file) {
    if (!file || isTablet) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(reader.result);
        if (!data || data.format !== 'live_notes') throw new Error('format');
        if (data.version > PROJECT_VERSION) throw new Error('version');
        var loaded = projectLayers(data);
        if (!loaded) throw new Error('format');
        if (recording) stopRecording();
        if (previewing) stopPreview();

        // Armé avant tout le reste : `createWorkspace` change le format du
        // canevas, ce qui repose la question du cadrage. Le projet y a déjà
        // répondu, la fenêtre n'a donc pas à s'ouvrir en travers du
        // chargement.
        pendingProject = projectMediaToRestore(data);

        var next = data.config || {};
        createWorkspace({
          width: parseInt(next.width, 10) || 1920,
          height: parseInt(next.height, 10) || 1080,
          fps: parseInt(next.fps, 10) || 30,
          alpha: !!next.alpha,
          background: next.background || '#ffffff'
        });
        adoptLayers(loaded, data.activeLayer);
        recordedDuration = Number(data.durationMs) || 0;

        // Afficher la barre de progression pendant la reconstruction des tracés.
        var loadingBar = $('project-loading-bar');
        var loadingOverlay = $('project-loading');
        loadingBar.style.width = '0%';
        loadingOverlay.style.display = 'flex';

        rebuildFinalCanvasAsync(function (ratio) {
          loadingBar.style.width = Math.round(ratio * 100) + '%';
          if (ratio < 1) return;
          // Reconstruction terminée.
          loadingOverlay.style.display = 'none';
          updateExportState();
          publishConfig();
          restoreProjectMedia(data);
        });
      } catch (e) {
        pendingProject = null;
        // « Trop récent » et « pas un projet » ne se rattrapent pas de la même
        // façon : l'un demande une mise à jour, l'autre un autre fichier.
        status(e && e.message === 'version'
          ? 'Projet enregistré par une version plus récente de live_notes.'
          : 'Projet .lvn invalide.');
      }
    };
    reader.onerror = function () { status('Impossible de lire le projet.'); };
    reader.readAsText(file);
  }

  /* Réouverture du média d'un projet.
   *
   * Un tracé a été posé sur une image précise, à un cadrage précis, entre deux
   * bornes précises. Rouvrir le projet sans elles laissait un dessin qui
   * flotte au-dessus du vide, et la seule issue était de retrouver le fichier
   * à la main puis de refaire tous les réglages. On redemande donc au serveur
   * le fichier noté dans le projet — il est à son dernier emplacement connu —
   * et on repose dessus la page, le cadrage et les bornes enregistrés.
   *
   * Le fichier a pu être déplacé, renommé, ou vivre sur un disque qui n'est
   * pas branché : on le dit alors clairement, en donnant le chemin cherché, et
   * le reste du projet est chargé quand même. */
  var pendingProject = null;

  /** Ce qu'il y a à restaurer, tiré du fichier de projet. `null` si le projet
   *  n'avait pas de média connu du serveur (média ouvert depuis le navigateur,
   *  ou tracé sur le fond seul). */
  function projectMediaToRestore(data) {
    var media = data && data.media;
    if (!media || !media.sourcePath) return null;
    var timed = typeof data.inPoint === 'number' && typeof data.outPoint === 'number'
      && data.outPoint > data.inPoint;
    return {
      name: media.name,
      sourcePath: media.sourcePath,
      page: media.page || null,
      fit: media.fit || null,
      range: timed ? { in: data.inPoint, out: data.outPoint } : null
    };
  }

  function restoreProjectMedia(data) {
    var wanted = pendingProject;
    if (!wanted) {
      // Rien à rouvrir : soit le projet n'avait pas de média, soit il venait
      // du sélecteur du navigateur, qui ne donne qu'un blob anonyme — le
      // serveur n'en a jamais connu le chemin.
      status(data.media
        ? 'Projet chargé — rechargez le média « ' + data.media.name
          + ' » à la main : ce projet ne garde pas son emplacement.'
        : 'Projet chargé — aucun média n\u2019était enregistré avec lui.');
      return;
    }

    // Déjà à l'écran : il n'y a rien à redemander au serveur, seulement à
    // reposer le cadrage et les bornes du projet sur le média en place.
    if (mediaIdentity(currentMedia) === mediaIdentity(wanted)) {
      pendingProject = null;
      if (wanted.fit) { mediaFit = cloneFit(wanted.fit); applyMediaFit(); }
      answeredFitQuestion = mediaFitQuestion();
      if (wanted.range && MediaTransport.isTimed()) {
        MediaTransport.setRange(wanted.range.in, wanted.range.out);
      }
      publishConfig();
      status('Projet chargé — média « ' + wanted.name + ' » retrouvé.');
      return;
    }

    status('Projet chargé — réouverture de « ' + wanted.name + ' »…');
    fetch('/api/media/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: wanted.sourcePath, page: wanted.page || undefined })
    })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (!payload.error && !(payload.media && payload.media.state === 'error')) return;
        // Le média redescend par l'état de session : c'est `applyMedia` qui
        // reposera cadrage et bornes. Ici, seul l'échec demande une réponse.
        pendingProject = null;
        status('Média « ' + wanted.name + ' » introuvable à son dernier '
          + 'emplacement (' + wanted.sourcePath + ') — le tracé est chargé, '
          + 'rouvrez le média à la main.');
      })
      .catch(function () {
        pendingProject = null;
        status('Serveur injoignable — le tracé est chargé, rouvrez « '
          + wanted.name + ' » à la main.');
      });
  }

  $('btn-save-project').addEventListener('click', saveProject);
  $('btn-load-project').addEventListener('click', function () { $('project-input').click(); });
  $('project-input').addEventListener('change', function (event) {
    loadProject(event.target.files[0]);
    event.target.value = '';
  });

  /* Sélecteur de dossier natif : c'est le serveur local qui l'ouvre, le
   * navigateur n'a pas le droit de donner un chemin de disque. */
  $('export-browse').addEventListener('click', function () {
    var button = $('export-browse');
    button.disabled = true;
    button.textContent = 'Sélection…';

    fetch('/api/browse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ directory: $('export-dir').value })
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.directory) $('export-dir').value = data.directory;
        else if (data.error) status(data.error);
      })
      .catch(function () { status('Sélecteur de dossier indisponible.'); })
      .then(function () {
        button.disabled = false;
        button.textContent = 'Parcourir…';
      });
  });

  $('export-width').addEventListener('input', function () {
    var width = parseInt($('export-width').value, 10);
    if (!width) return;
    $('export-height').value = Math.round(width * config.height / config.width);
  });

  $('export-mode').addEventListener('change', function () {
    // Le nom par défaut change de nature avec le mode : un dossier porte le nom
    // du média, un fichier celui du rendu daté. On ne réécrit que celui qu'on
    // avait proposé — un nom saisi à la main reste.
    var pro = $('export-mode').value === 'pro';
    var current = $('export-name').value.trim();
    if (!current || current === lastProposedName) {
      $('export-name').value = pro ? defaultFolderName() : defaultExportName();
      lastProposedName = $('export-name').value;
    }
    syncExportDialog();
  });

  $('export-flatten').addEventListener('change', syncExportDialog);
  $('export-name').addEventListener('input', syncExportDialog);

  $('export-cancel').addEventListener('click', function () {
    $('export-screen').style.display = 'none';
  });

  $('export-go').addEventListener('click', function () {
    $('export-screen').style.display = 'none';
    var mode = $('export-mode').value;
    var out = outPoint();
    startExport({
      width: config.width,
      height: config.height,
      outWidth: parseInt($('export-width').value, 10) || config.width,
      outHeight: parseInt($('export-height').value, 10) || config.height,
      fps: parseInt($('export-fps').value, 10) || config.fps,
      mode: mode,
      flatten: $('export-flatten').value,
      mediaAlpha: $('export-media-alpha').checked,
      // Le damier est un artifice d'écran : il n'a pas de couleur à exporter,
      // et un export sans alpha doit retomber sur le blanc historique.
      background: config.background === CHECKER ? '#ffffff' : config.background,
      filename: $('export-name').value,
      folder: $('export-name').value,
      directory: $('export-dir').value,
      durationMs: duration(),
      // Une couche masquée n'est pas envoyée : masquer et exclure de l'export
      // sont la même notion, pas deux réglages qui pourraient se contredire.
      layers: layers.filter(function (layer) { return layer.visible; })
        .map(function (layer) {
          return { strokes: layer.strokes, clears: layer.clears };
        }),
      // Ce que le canevas montrait sous le tracé : le cadrage du média, et la
      // portion qui a été jouée. Le point IN est l'origine des temps du tracé,
      // donc celle de toutes les couches.
      mediaFit: mediaFit,
      mediaIn: inPoint(),
      mediaOut: isFinite(out) ? out : 0
    });
  });

  var currentJob = null;
  var pollTimer = null;

  function startExport(payload) {
    var overlay = $('render-overlay');
    overlay.style.display = 'flex';
    $('render-title').textContent = 'Rendu en cours…';
    $('render-detail').textContent = 'Envoi des tracés au moteur Python…';
    $('progress-bar').style.width = '0%';
    $('render-cancel').disabled = false;

    fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (response) { return response.json(); })
      .then(function (job) {
        if (job.error) throw new Error(job.error);
        currentJob = job.jobId;
        pollTimer = setInterval(pollJob, 350);
      })
      .catch(function (error) { failExport(error.message || 'Serveur Python injoignable.'); });
  }

  function pollJob() {
    if (!currentJob) return;
    fetch('/api/export/' + currentJob)
      .then(function (response) { return response.json(); })
      .then(function (job) {
        if (job.state === 'running') {
          $('progress-bar').style.width = job.percent + '%';
          // Un export pro enchaîne trois rendus : dire lequel est en cours vaut
          // mieux qu'une barre qui repart à zéro sans explication.
          $('render-title').textContent = job.steps > 1
            ? 'Rendu — ' + job.label + ' (' + job.step + '/' + job.steps + ')'
            : 'Rendu en cours…';
          $('render-detail').textContent =
            'Image ' + job.frame + ' / ' + job.totalFrames + ' — ' + job.percent + ' %';
          return;
        }
        stopPolling();
        if (job.state === 'done') {
          $('render-overlay').style.display = 'none';
          var files = (job.files || []).length > 1
            ? '\n\n' + job.files.map(function (path) {
              return '· ' + path.split(/[\\/]/).pop();
            }).join('\n') : '';
          status('Export terminé : ' + job.filepath);
          alert('Export réussi !\n\n' + job.filepath + files);
        } else if (job.state === 'cancelled') {
          $('render-overlay').style.display = 'none';
          status('Export annulé.');
        } else {
          failExport(job.error || 'Erreur inconnue.');
        }
      })
      .catch(function () { failExport('Perte de contact avec le serveur Python.'); });
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    currentJob = null;
  }

  function failExport(message) {
    stopPolling();
    $('render-overlay').style.display = 'none';
    status('Échec de l’export : ' + message);
    alert('Échec de l’export :\n\n' + message);
  }

  $('render-cancel').addEventListener('click', function () {
    if (!currentJob) return;
    $('render-cancel').disabled = true;
    $('render-detail').textContent = 'Annulation…';
    fetch('/api/export/' + currentJob + '/cancel', { method: 'POST' });
  });

  /* ------------------------------------------------------------ Divers */

  $('prop-color').addEventListener('input', function (event) {
    currentBrush.color = event.target.value;
    onBrushChanged();
  });

  $('btn-save-preset').addEventListener('click', savePreset);

  $('btn-reset-brush').addEventListener('click', function () {
    var source = library.presets.concat(library.userPresets).filter(function (preset) {
      return preset.id === currentBrush.id;
    })[0];
    if (!source) return;
    delete brushEdits[source.id];
    persistEdits();
    selectBrush(source);
    renderLibrary();
    status('Pinceau « ' + (source.name || source.id) + ' » réinitialisé.');
  });

  $('btn-delete-brush').addEventListener('click', function () {
    var preset = library.userPresets.filter(function (item) {
      return item.id === currentBrush.id;
    })[0];
    if (preset) confirmDelete(preset);
  });

  $('btn-texture').addEventListener('click', function () { $('texture-input').click(); });

  $('texture-input').addEventListener('change', function (event) {
    var file = event.target.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      BE.loadTexture(reader.result).then(function () {
        BE.clearCache();
        currentBrush.shape = 'texture';
        currentBrush.texture = reader.result;
        onBrushChanged();
        renderLibrary();
        status('Texture chargée — enregistrez un preset pour la conserver.');
      }).catch(function () { status('Texture illisible.'); });
    };
    reader.readAsDataURL(file);
  });

  /* ==================================================================== */
  /*                            MODE TABLETTE                             */
  /* ==================================================================== */

  /* Trois choses circulent, et rien d'autre : l'état de session (qui contrôle
   * quoi, où en est le média), les gestes de dessin, et les commandes de
   * lecture. Le média lui-même ne circule pas — les deux côtés le lisent
   * depuis le serveur, chacun à son rythme. */

  /* ------------------------------------------- Média servi par le serveur */

  /** Ouvre le sélecteur natif du serveur. C'est ce détour qui permet de
   *  prendre n'importe quel codec : Python obtient un vrai chemin de fichier,
   *  donc peut transcoder un proxy lisible (voir proxy.py). Si le sélecteur
   *  n'est pas disponible, on retombe sur le champ fichier du navigateur. */
  function openMediaOnServer(fallbackId) {
    var fallback = function () {
      status('Sélecteur natif indisponible — repli sur le navigateur '
        + '(seuls les codecs reconnus par lui seront lisibles).');
      $(fallbackId).click();
    };
    status('Sélection du média…');
    fetch('/api/media/pick', { method: 'POST' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.cancelled) { status('Sélection annulée.'); return; }
        if (data.error) { fallback(); return; }
        // Un PDF n'est pas encore ouvert : le serveur attend de savoir quelle
        // page rasteriser.
        if (data.pdf) { openPdfChooser(data.pdf); return; }
        if (data.media && data.media.state === 'error') status(data.media.error || 'Média illisible.');
      })
      .catch(fallback);
  }

  /* ------------------------------------------------- Choix de page (PDF) */

  /* Une fois la page choisie, elle est rasterisée côté serveur et redescend
   * comme une image ordinaire : rien en aval — transport, cadrage, tablette —
   * n'a besoin de savoir qu'il s'agissait d'un PDF. */

  var pdfDraft = null;

  function openPdfChooser(pdf) {
    if (recording || previewing) {
      status('Impossible de changer de média pendant l’enregistrement ou la prévisualisation.');
      return;
    }
    pdfDraft = { name: pdf.name, pageCount: pdf.pageCount, page: pdf.page || 1 };
    $('pdf-hint').textContent = 'Document « ' + pdf.name + ' » — '
      + pdf.pageCount + (pdf.pageCount > 1 ? ' pages.' : ' page.')
      + ' La page choisie devient l’image de fond.';
    $('pdf-page').max = pdf.pageCount;
    $('pdf-count').textContent = '/ ' + pdf.pageCount;
    syncPdfChooser();
    $('pdf-screen').style.display = 'flex';
  }

  function closePdfChooser() {
    $('pdf-screen').style.display = 'none';
    // L'aperçu pèse quelques centaines de kilo-octets : inutile de le garder
    // en mémoire une fois la fenêtre fermée.
    $('pdf-preview').removeAttribute('src');
    pdfDraft = null;
  }

  function syncPdfChooser() {
    if (!pdfDraft) return;
    $('pdf-page').value = pdfDraft.page;
    $('pdf-prev').disabled = pdfDraft.page <= 1;
    $('pdf-next').disabled = pdfDraft.page >= pdfDraft.pageCount;
    $('pdf-preview').src = '/api/pdf/page/' + pdfDraft.page + '.png';
  }

  function stepPdfPage(delta) {
    if (!pdfDraft) return;
    pdfDraft.page = clamp(pdfDraft.page + delta, 1, pdfDraft.pageCount);
    syncPdfChooser();
  }

  $('pdf-prev').addEventListener('click', function () { stepPdfPage(-1); });
  $('pdf-next').addEventListener('click', function () { stepPdfPage(1); });

  $('pdf-page').addEventListener('input', function (event) {
    if (!pdfDraft) return;
    var wanted = parseInt(event.target.value, 10);
    if (!isFinite(wanted)) return;
    pdfDraft.page = clamp(wanted, 1, pdfDraft.pageCount);
    syncPdfChooser();
  });

  $('pdf-cancel').addEventListener('click', function () {
    closePdfChooser();
    status('Sélection annulée.');
  });

  $('pdf-open').addEventListener('click', function () {
    if (!pdfDraft) return;
    var page = pdfDraft.page;
    var name = pdfDraft.name;
    closePdfChooser();
    status('Rasterisation de la page ' + page + '…');
    fetch('/api/media/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // Pas de chemin : le serveur sait de quel PDF il s'agit, on ne
      // désigne que la page.
      body: JSON.stringify({ page: page })
    }).then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.error) status(data.error);
        else if (data.media && data.media.state === 'error') {
          status(data.media.error || 'Page illisible.');
        }
      })
      .catch(function () { status('Ouverture de « ' + name + ' » impossible.'); });
  });

  /* Changer de page après coup : le PDF est resté connu du serveur. */
  $('tp-page').addEventListener('click', function () {
    if (recording || previewing) {
      status('Impossible de changer de page pendant l’enregistrement ou la prévisualisation.');
      return;
    }
    if (!currentMedia || !currentMedia.pageCount) return;
    openPdfChooser({
      name: currentMedia.documentName || currentMedia.name,
      pageCount: currentMedia.pageCount,
      page: currentMedia.page || 1
    });
  });

  /** URL de lecture, complétée du jeton quand on n'est pas sur le poste. */
  function mediaUrlFor(media) {
    if (!media || !media.url) return null;
    var url = media.url + '?v=' + media.id;
    if (isTablet && REMOTE.token) url += '&k=' + encodeURIComponent(REMOTE.token);
    return url;
  }

  /** Le navigateur décode-t-il encore le rush qu'on vient de lui donner ?
   *  Entre l'installation et les premières métadonnées il peut s'écouler
   *  plusieurs secondes pendant lesquelles rien ne bouge à l'écran. */
  function setMediaLoading(active) {
    // Le décodage reste signalé dans la ligne d'état, sans pastille superposée
    // au rush.
  }

  /** Badge « proxy » / « original ». `null` masque : aucun rush à qualifier. */
  function setMediaSourceBadge(proxied) {
    var badge = $('media-source-badge');
    if (!badge) return;
    badge.hidden = proxied === null;
    if (badge.hidden) return;
    badge.textContent = proxied ? 'proxy' : 'original';
    badge.classList.toggle('is-proxy', !!proxied);
    badge.title = proxied
      ? 'Copie de lecture — l’export reste calculé à partir du rush d’origine'
      : 'Le rush d’origine, servi tel quel';
  }

  var appliedMediaKey = null;
  var currentMedia = null;
  var rescuedMediaKey = null;   /* média déjà rattrapé : on ne boucle pas */

  /* Ce qui identifie le rush lui-même, et non la copie qu'on en sert.
   *
   * Deux préparations de la même source — l'original, son proxy, un
   * transcodage qui vient de s'achever — la désignent à l'identique, alors
   * qu'elles portent trois identifiants de média différents. C'est ce qui
   * distingue « le même rush sous un autre URL » de « un autre rush », et les
   * deux n'appellent pas du tout le même traitement. La page compte : deux
   * pages d'un PDF sont deux images. */
  function mediaIdentity(media) {
    if (!media) return null;
    var source = media.sourcePath || media.name;
    return source ? source + '#' + (media.page || 0) : null;
  }

  /* Bornes IN/OUT à poser sur le rush que l'on installe.
   *
   * Trois cas, et ils ne se confondent pas. Un projet en cours d'ouverture
   * impose les siennes : ce sont celles qui ont été enregistrées avec le
   * tracé. Le même rush reservi sous un autre identifiant garde celles qui
   * sont posées dessus — c'est le cas de la bascule proxy ↔ original. Un
   * *autre* rush repart de sa plage entière : les bornes du précédent ne
   * décrivent rien chez lui, et les lui appliquer découperait au hasard.
   *
   * Sur la tablette, la référence n'est jamais son propre lecteur mais ce que
   * le poste a publié : c'est lui l'arbitre des bornes. */
  function rangeForNewMedia(media, previous) {
    if (pendingProject && pendingProject.range) return pendingProject.range;
    if (isTablet) {
      return sessionRange ? { in: sessionRange.in, out: sessionRange.out } : null;
    }
    var identity = mediaIdentity(media);
    if (!identity) return null;
    if (identity === mediaIdentity(previous)) return currentRange();
    // Le rush revient d'un aller-retour par l'export, qui l'avait lâché.
    if (releasedBounds && identity === releasedBounds.identity) return releasedBounds.range;
    return null;
  }

  /** Bornes du dernier rush lâché, gardées le temps qu'il revienne. */
  var releasedBounds = null;

  function applyMedia(media) {
    var key = media && media.state === 'ready' ? media.id : null;
    if (media && media.name) $('setup-media-name').value = media.name;

    if (media && media.state === 'preparing') {
      var percent = Math.round(media.progress || 0);
      status('Préparation du média « ' + media.name + ' » — ' + percent + ' %'
        + (media.reason ? ' (' + media.reason + ')' : ''));
      $('media-prep').style.display = 'flex';
      $('media-prep-name').textContent = media.name;
      $('media-prep-reason').textContent = media.reason || '';
      $('media-prep-bar').style.width = percent + '%';
      $('media-prep-percent').textContent = percent + ' %';
      return;
    }
    $('media-prep').style.display = 'none';

    if (media && media.state === 'error') {
      status('Média refusé : ' + (media.error || 'illisible'));
      return;
    }
    if (media && media.state === 'released') {
      // Le proxy a été effacé au lancement de l'export : on lâche le lecteur,
      // sinon le fichier resterait ouvert et le disque occupé.
      if (appliedMediaKey) {
        // Le rush va revenir, refabriqué, sous un autre identifiant : ses
        // bornes sont mises de côté le temps de l'aller-retour. Sans cela,
        // chaque export reposait la plage entière sur le rush qu'on venait
        // justement de découper.
        releasedBounds = { identity: mediaIdentity(currentMedia), range: currentRange() };
        releaseMedia();
        MediaTransport.detach();
        appliedMediaKey = null;
        currentMedia = null;
        answeredFitQuestion = null;
      }
      return;
    }

    if (key === appliedMediaKey) return;
    appliedMediaKey = key;
    // Plus de média : l'export ne peut plus en composer une couche, et le
    // cadrage qui vient d'être remis à zéro ne décrit plus rien. Reposer le
    // même rush ensuite doit reposer la question du cadrage.
    if (!key) {
      currentMedia = null;
      answeredFitQuestion = null;
      releaseMedia();
      MediaTransport.detach();
      return;
    }

    // Le cadrage en cours, avant que `installMedia` ne le remette à zéro.
    // Préparer un proxy republie le *même* rush sous un nouvel identifiant :
    // le rush réapparaissait alors non recadré, et la fenêtre de cadrage
    // redemandait un choix déjà fait. La transformation choisie survit donc à
    // une nouvelle préparation de la même image — c'est bien la même question,
    // et elle a déjà sa réponse.
    var settledFit = cloneFit(mediaFit);
    var previousMedia = currentMedia;
    currentMedia = media;
    var sameQuestion = mediaFitQuestion() !== null
      && mediaFitQuestion() === answeredFitQuestion;

    var savedRange = rangeForNewMedia(media, previousMedia);

    installMedia(mediaUrlFor(media), media.name, media.kind, false, savedRange);

    if (pendingProject) {
      // Le projet qu'on ouvre impose son cadrage : `installMedia` vient de le
      // remettre à zéro, on repose celui qui a été enregistré avec le tracé.
      // La géométrie suivra à l'arrivée des métadonnées (`onMediaReady`), et la
      // question du cadrage a désormais sa réponse — la fenêtre ne s'ouvrira
      // pas par-dessus un choix déjà fait.
      if (pendingProject.fit) mediaFit = cloneFit(pendingProject.fit);
      answeredFitQuestion = mediaFitQuestion();
      status('Projet chargé — média « ' + pendingProject.name + ' » rouvert.');
      pendingProject = null;
      if (!isTablet) publishConfig();
    } else if (sameQuestion) {
      // Avant `onMediaReady` (déclenché par les métadonnées, donc plus tard) :
      // il pose la géométrie à partir de `mediaFit`, qui doit déjà être la
      // bonne. `publishConfig` renvoie à la tablette ce que `releaseMedia`
      // vient de lui annoncer remis à zéro.
      mediaFit = settledFit;
      if (!isTablet) publishConfig();
    }
    setMediaSourceBadge(!!media.proxied);
    // Seul un PDF a des pages à parcourir, et seul le poste choisit.
    $('tp-page').style.display = (!isTablet && media.pageCount) ? '' : 'none';
    if (media.proxied) {
      status('Média « ' + media.name + ' » chargé — proxy de lecture ('
        + media.reason + (media.cached ? ', repris du cache' : '')
        + '). L’export reste calculé à pleine résolution.');
    }
    // Un transcodage qui vient de s'achever change le contenu du cache.
    if (!isTablet && $('proxy-panel').style.display === 'flex') refreshProxies();
  }

  /* ---------------------------------------------------- État de session */

  var tabletHadControl = false;   /* pour ne fermer l'appairage qu'à la bascule */

  function applySessionState(state) {
    sessionMode = state.mode;
    controller = state.controller || 'pc';
    if ((isTablet || controller === 'tablet') && $('crop-screen').style.display === 'flex') closeCrop();
    if (state.config) syncConfigFromSession(state.config);
    applyMedia(state.media);

    // Un seul et même critère des deux côtés : « quelqu'un d'autre a le
    // stylet ». Le poste pendant que la tablette dessine, la tablette pendant
    // que le poste a repris la main — c'est le même état, donc le même code.
    setViewing(!inControl());

    if (isTablet) {
      $('tablet-badge').style.display = 'flex';
      updateTabletBadge();
      syncTabletControlButton();
      return;
    }

    // La tablette vient de prendre la main : l'appairage a abouti, et le QR
    // code n'a plus rien à dire. On rend le canevas au poste — qui devient un
    // écran de contrôle — au lieu de laisser une fenêtre à refermer à la main.
    // Sur la transition seulement : rouvrir l'appairage ensuite, pour reprendre
    // le stylet, doit rester possible.
    var handedOver = state.mode === 'live' && controller === 'tablet';
    if (handedOver && !tabletHadControl) $('tablet-screen').style.display = 'none';
    tabletHadControl = handedOver;

    $('btn-tablet').classList.toggle('armed', state.mode !== 'solo');
    $('tablet-state').textContent = state.mode !== 'live'
      ? (state.mode === 'pairing' ? 'En attente de la tablette…' : 'Mode tablette désactivé.')
      : (controller === 'tablet'
        ? 'Tablette connectée — elle a la main.'
        : 'Tablette connectée — elle suit ce poste.');

    // « Reprendre » et « rendre » sont exclusifs, et ne valent que pendant une
    // session vivante ; « terminer » est le seul bouton qui rompt le lien.
    var live = state.mode === 'live';
    $('tablet-take').style.display = live && controller === 'tablet' ? '' : 'none';
    $('tablet-give').style.display = live && controller === 'pc' ? '' : 'none';
    $('tablet-end').style.display = state.mode === 'solo' ? 'none' : '';
  }

  /** La tablette adopte le format de travail publié par le poste. */
  function syncConfigFromSession(next) {
    if (!isTablet) return;
    if ($('crop-screen').style.display === 'flex') closeCrop();
    var changed = next.width !== config.width || next.height !== config.height
      || next.fps !== config.fps || next.alpha !== config.alpha;
    if (!tabletReady || changed) {
      // Changer de format en cours de dessin invaliderait les tracés déjà
      // posés : ils sont exprimés dans l'ancien référentiel.
      if (tabletReady && changed && strokeCount()) resetLayers();
      createWorkspace(next);
      tabletReady = true;
    }

    // Le fond et le cadrage ne touchent pas au référentiel des tracés : ils se
    // rejouent sans rien invalider, y compris quand le format n'a pas bougé —
    // c'est le cas courant, on recadre bien plus souvent qu'on ne change de
    // format de travail.
    if (next.background && next.background !== config.background) {
      adoptBackground(next.background);
    }
    if (next.mediaFit) {
      sessionFit = cloneFit(next.mediaFit);
      adoptMediaFit(sessionFit);
    }
    // Bornes IN/OUT publiées par le poste. Mémorisées même quand le média
    // n'est pas encore chargé : `onMediaReady` les reposera une fois la durée
    // connue, sans quoi `setRange` les écrêterait à une durée nulle.
    if (typeof next.inPoint === 'number' && typeof next.outPoint === 'number') {
      sessionRange = { in: next.inPoint, out: next.outPoint };
      if (MediaTransport.isTimed()) MediaTransport.setRange(sessionRange.in, sessionRange.out);
    }
  }

  var sessionRange = null;

  /** Cadrage arbitré par le poste : la tablette l'applique tel quel. */
  function adoptMediaFit(next) {
    if (mediaFit.mode === next.mode && mediaFit.zoom === next.zoom
      && mediaFit.posX === next.posX && mediaFit.posY === next.posY) return;
    mediaFit = cloneFit(next);
    applyMediaFit();
  }

  function setViewing(active) {
    var changed = viewing !== active;
    viewing = active;
    document.body.classList.toggle('viewing', active);
    $('viewer-overlay').style.display = active ? 'flex' : 'none';
    $('viewer-label').textContent = isTablet
      ? 'Le poste a la main' : 'Contrôle en cours sur tablette';
    // Rendre la main à la tablette se fait depuis le poste, jamais en se
    // servant soi-même : c'est ce qui garde un arbitre unique.
    $('viewer-takeover').style.display = isTablet ? 'none' : '';
    applyAudioRole();
    if (!changed) return;
    if (active) {
      hideCursor();
      if (drawing) onPointerUp({ pointerId: -1 });
      sealIncoming();
      status(isTablet
        ? 'Le poste a repris la main — cet écran suit en direct.'
        : 'Contrôle en cours sur tablette — cet écran suit en direct.');
    } else {
      status(isTablet ? 'Vous avez la main.' : 'Contrôle repris sur le poste.');
    }
    updateExportState();
  }

  /** Pourquoi cette action est refusée, dit du point de vue de qui la tente. */
  function handOverNotice(verb) {
    return isTablet
      ? ('Le poste a la main — il doit vous la rendre pour ' + verb + '.')
      : ('La tablette a la main — reprenez le contrôle pour ' + verb + '.');
  }

  /** Un seul haut-parleur à la fois.
   *
   *  Les deux écrans lisent le même rush, chacun depuis son propre lecteur :
   *  sans cette règle on entend la bande-son en double, décalée de la latence
   *  du réseau. Le son suit le stylet — celui qui dessine est celui qui a
   *  besoin d'entendre où il en est. */
  function applyAudioRole() {
    if (!mediaNode || mediaType === 'image') return;
    mediaNode.muted = viewing;
  }

  /* --------------------------------------------- Réception des gestes */

  function handleRemote(message) {
    switch (message.t) {
      case 'stroke.begin': return remoteStrokeBegin(message);
      case 'stroke.points': return remoteStrokePoints(message);
      case 'stroke.end': return remoteStrokeEnd(message);
      case 'stroke.abort': return remoteStrokeAbort(message);
      case 'transport': return remoteTransport(message);
      case 'rec.start': return remoteRecStart();
      case 'rec.stop': return remoteRecStop(message);
      case 'clear': return remoteClear(message);
      case 'reset': return remoteReset();
      case 'layers': return remoteLayers(message);
      case 'undo': return undo(true);
      case 'redo': return redo(true);
      case 'brush': return remoteBrush(message);
      case 'range': return remoteRange(message);
      case 'control': return remoteControl(message);
      case 'peers':
        updateTabletBadge();
        // Une tablette qui arrive ne connaît pas encore la pile : le poste la
        // lui dit. Le contenu déjà dessiné, lui, ne se rattrape pas — c'était
        // déjà le cas des traces avant les couches.
        publishLayers();
        return undefined;
      default: return undefined;
    }
  }

  function remoteStrokeBegin(message) {
    var brush = BE.normalize(message.brush);
    // Même pinceau, même graine, même moteur : le tracé reconstruit ici est
    // identique au pixel près à celui affiché sous le stylet.
    var trace = makeTrace({ brush: brush, seed: message.seed, points: [message.point] });
    incoming[message.id] = trace;
    feedTrace(trace, Infinity);
    requestRedraw();
  }

  function remoteStrokePoints(message) {
    var trace = incoming[message.id];
    if (!trace) return;
    for (var i = 0; i < message.points.length; i++) trace.points.push(message.points[i]);
    feedTrace(trace, Infinity);
    requestRedraw();
  }

  function remoteStrokeEnd(message) {
    var trace = incoming[message.id];
    if (!trace) return;
    delete incoming[message.id];
    feedTrace(trace, Infinity);
    commitTrace(trace, baseCtx);
    // Le poste accumule le tracé complet : c'est lui qui exportera, avec
    // exactement la même charge utile qu'en solo.
    recordStroke(activeLayer(), { brush: trace.brush, seed: trace.seed, points: trace.points });
    requestRedraw();
    updateExportState();
  }

  /** Le trait a été abandonné là-bas (un pincement l'a interrompu) : il n'est
   *  pas terminé, il n'a jamais existé. */
  function remoteStrokeAbort(message) {
    if (!incoming[message.id]) return;
    delete incoming[message.id];
    requestRedraw();
  }

  /* Une tablette qui se déconnecte au milieu d'un tracé laisserait un calque
   * orphelin visible mais jamais fusionné. On le termine proprement. */
  function sealIncoming() {
    Object.keys(incoming).forEach(function (id) { remoteStrokeEnd({ id: id }); });
  }

  function remoteTransport(message) {
    if (!mediaNode || mediaType === 'image') return;
    if (typeof message.time === 'number') {
      // On ne repositionne que si l'écart se voit : corriger en permanence
      // ferait saccader la lecture sur le poste.
      var drift = Math.abs((mediaNode.currentTime || 0) - message.time);
      if (message.action !== 'time' || drift > 0.3) mediaNode.currentTime = message.time;
    }
    if (message.action === 'play' && mediaNode.paused) mediaNode.play().catch(function () { });
    if (message.action === 'pause' && !mediaNode.paused) mediaNode.pause();
  }

  /* Bornes IN/OUT venues de l'autre écran. Elles décrivent *quand* le tracé
   * commence et finit : deux écrans qui ne les partagent pas annotent le même
   * rush sur deux minutages différents, et le tracé ne retombe plus en face à
   * l'export. `setRange` ne rappelle rien quand les valeurs ne bougent pas,
   * ce qui empêche l'aller-retour de reboucler. */
  function remoteRange(message) {
    if (typeof message.in !== 'number' || typeof message.out !== 'number') return;
    MediaTransport.setRange(message.in, message.out);
  }

  /* Seul le poste peut piloter l'appairage : la tablette demande, il exécute. */
  function remoteControl(message) {
    if (isTablet) return;
    if (message.want === 'pc') sessionAction('take');
    else if (message.want === 'tablet') sessionAction('give');
  }

  function remoteRecStart() {
    // Le REC de l'autre écran ne vide que la couche active, comme ici — les
    // couches verrouillées se rejouent dessous des deux côtés.
    clearActiveLayer();
    startUnderlay();
    recording = true;
    underlayStart = performance.now();
    if (replay) recFrame = requestAnimationFrame(recTick);
    // Le lecteur de cet écran suit celui qui enregistre. Sans le verrou, sa
    // propre borne de sortie l'arrêterait de son côté pendant que l'autre
    // continue : les deux écrans cesseraient de montrer la même image, et le
    // transport resterait manipulable au milieu d'une prise.
    if (MediaTransport.isTimed()) MediaTransport.setLocked(true);
    document.body.classList.add('recording');
    status('Enregistrement en cours sur la tablette…');
  }

  /* La fin d'enregistrement ne termine pas les tracés en cours.
   *
   * Le rush s'arrête sur son point OUT, mais la main, elle, ne s'arrête pas :
   * le geste continue sur l'image figée, et c'est même à cela que sert le gel —
   * annoter la dernière image d'un plan demande plus de temps qu'elle n'en
   * dure. Ces points-là continuent d'arriver, sous l'identifiant du tracé
   * commencé avant le point OUT.
   *
   * Sceller ici les tracés en cours jetait leur calque : les points suivants
   * n'avaient plus de destination et étaient ignorés un à un. Sur la tablette
   * le trait restait continu ; à l'arrivée il était coupé net, à l'instant
   * précis de la fin de lecture. Un tracé commencé *après*, lui, passait
   * entier — il ouvrait son propre calque. C'est ce qui rendait la panne
   * déroutante : le REC n'était pas coupé, un seul trait l'était.
   *
   * Un tracé se termine par son `stroke.end`, et par rien d'autre. Le scellage
   * reste ce qu'il doit être : le rattrapage d'une tablette qui s'en va
   * (déconnexion, reprise de main) et ne dira jamais qu'elle a fini. */
  function remoteRecStop(message) {
    recording = false;
    recordedDuration = Math.max(recordedDuration, message.duration || 0);
    if (activeLayer()) {
      activeLayer().durationMs = Math.max(activeLayer().durationMs || 0, message.duration || 0);
    }
    stopUnderlay();
    if (MediaTransport.isTimed()) {
      MediaTransport.setLocked(false);
      MediaTransport.parkAtOut();
    }
    document.body.classList.remove('recording');
    updateExportState();
    status('Enregistrement terminé sur la tablette — ' + strokeCount() + ' tracé(s).');
  }

  function remoteClear(message) {
    applyClear(activeLayer(), message.at);
    Object.keys(incoming).forEach(function (id) {
      incoming[id].ctx.clearRect(0, 0, canvas.width, canvas.height);
    });
    requestRedraw();
  }

  function remoteReset() {
    incoming = {};
    clearActiveLayer();
  }

  /** Le poste structure, la tablette suit : la pile de couches voyage en
   *  entier plutôt qu'en commandes, ce qui interdit aux deux écrans de
   *  diverger sur une commande perdue. Le contenu ne voyage pas — chaque
   *  écran a déjà le sien, par les tracés relayés. */
  function remoteLayers(message) {
    var list = message.list || [];
    if (!list.length) return;
    var known = {};
    layers.forEach(function (layer) { known[layer.id] = layer; });

    layers = list.map(function (item) {
      var layer = known[item.id];
      if (!layer) {
        layer = makeLayer(item.name);
        layer.id = item.id;
      }
      layerSeq = Math.max(layerSeq, item.id);
      layer.name = item.name;
      layer.visible = item.visible !== false;
      return layer;
    });

    activeId = layerById(message.activeId) ? message.activeId : layers[layers.length - 1].id;
    bindActiveLayer();
    syncLayerPanel();
    syncHistoryButtons();
    requestRedraw();
    updateExportState();
  }

  function remoteBrush(message) {
    // Purement informatif : le poste affiche ce que la tablette a en main.
    $('viewer-brush').textContent = message.name || '';
  }

  /* --------------------------------------------- Émission des commandes */

  function emit(message) {
    if (streaming()) REMOTE.send(message);
  }

  /** Le maître relaie ses commandes de lecture en écoutant son propre
   *  lecteur : play, pause et déplacements passent tous par là, quel que soit
   *  le bouton utilisé. Aucun couplage avec transport.js.
   *
   *  Installé des deux côtés, mais `emit` ne laisse passer que le maître : le
   *  suiveur applique ce qu'il reçoit sans jamais le renvoyer, ce qui suffit à
   *  écarter la boucle « je me repositionne donc j'annonce que je me suis
   *  repositionné ». */
  /* Les bornes viennent de changer sur cet écran.
   *
   * Deux destinataires, et ce ne sont pas les mêmes. L'autre écran d'abord,
   * par le canal temps réel — `emit` ne laisse passer que le maître, donc le
   * suiveur qui vient d'appliquer une borne reçue ne la renvoie pas. La
   * session ensuite, mais depuis le poste seul : c'est elle que lira une
   * tablette qui rejoindra plus tard. */
  function onRangeChanged() {
    emit({ t: 'range', in: MediaTransport.inPoint(), out: MediaTransport.outPoint() });
    if (!isTablet) publishConfig();
  }

  function watchTransportForRemote() {
    ['play', 'pause', 'seeked'].forEach(function (name) {
      document.addEventListener(name, function (event) {
        if (event.target !== mediaNode) return;
        emit({ t: 'transport', action: name === 'seeked' ? 'seek' : name,
               time: mediaNode.currentTime });
      }, true);
    });

    // Repère périodique pendant la lecture : suffit à rattraper la dérive
    // entre deux horloges sans inonder le canal.
    setInterval(function () {
      if (!mediaNode || mediaType === 'image' || mediaNode.paused) return;
      var now = performance.now();
      if (now - lastTimeSync < 500) return;
      lastTimeSync = now;
      emit({ t: 'transport', action: 'time', time: mediaNode.currentTime });
    }, 250);
  }

  /* ------------------------------------------------- Interface tablette */

  function updateTabletBadge() {
    if (!isTablet) return;
    // Un arrêt définitif prime sur tout le reste : le rafraîchissement
    // périodique ne doit pas remplacer « session fermée » par un rassurant
    // « reconnexion… » qui n'arrivera jamais.
    var stopped = REMOTE.fatal();
    if (stopped) {
      $('tablet-badge').classList.add('offline');
      $('tablet-link').textContent = stopped.detail || 'Session terminée.';
      showSessionOver(stopped.detail);
      return;
    }
    var online = REMOTE.isOnline();
    var ms = REMOTE.latency();
    $('tablet-badge').classList.toggle('offline', !online);
    $('tablet-link').textContent = !online
      ? 'Poste injoignable — reconnexion…'
      : ((viewing ? 'Le poste a la main' : 'Vous avez la main')
        + (ms !== null ? ' · ' + ms + ' ms' : ''));
  }

  /** Fin de partie sur la tablette : la session n'existe plus, et aucune
   *  reconnexion ne la ressuscitera. Autant le dire en grand plutôt que de
   *  laisser un canevas intact qui donne l'illusion qu'on peut continuer. */
  function showSessionOver(detail) {
    if (!isTablet) return;
    $('session-over-detail').textContent = detail
      || 'Le poste a fermé la session.';
    $('session-over').style.display = 'flex';
    status(detail || 'Session terminée.');
  }

  function updateViewerBadge() {
    var ms = REMOTE.latency();
    $('viewer-latency').textContent = ms !== null ? ms + ' ms' : '—';
  }

  /* --------------------------------------------------------- Appairage */

  /** Pourquoi l'appairage ne peut pas aboutir, dit dans la fenêtre elle-même.
   *
   *  Un 403 signifie que cet écran n'est pas le poste : les routes qui arment
   *  la session lui sont réservées. Le message compte, parce que la panne est
   *  autrement invisible — la fenêtre s'ouvrait sur un carré blanc, sans QR,
   *  sans URL et sans rien qui dise pourquoi. */
  function pairingFailed(response) {
    var message;
    if (!response) {
      message = 'Serveur injoignable : cette page n’a pas pu interroger la '
        + 'session. Vérifiez que live_notes tourne toujours dans son terminal, '
        + 'puis rechargez la page.';
    } else if (response.status === 403) {
      message = 'Cet écran n’est pas reconnu comme le poste. Le mode tablette se '
        + 'pilote depuis la fenêtre ouverte sur l’adresse locale (127.0.0.1), pas '
        + 'depuis une adresse réseau — celles-ci sont réservées à la tablette.';
    } else {
      // Le code brut plutôt qu'un « indisponible » qui n'apprend rien : c'est
      // lui qui dira si le serveur a planté sur la requête ou l'a refusée.
      message = 'Le serveur a refusé la demande d’appairage (HTTP '
        + response.status + ').';
    }
    $('tablet-qr').style.display = 'none';
    $('tablet-qr').innerHTML = '';
    $('tablet-url').textContent = '';
    $('tablet-nonet').style.display = 'none';
    $('tablet-state').textContent = message;
    status(message);
  }

  function refreshPairing() {
    fetch('/api/session')
      .then(function (response) {
        if (!response.ok) { pairingFailed(response); return null; }
        return response.json();
      }, function () {
        // Second argument plutôt qu'un `.catch` en fin de chaîne : celui-ci ne
        // voit que l'échec de la requête. Un `.catch` global attraperait aussi
        // les exceptions de `applySessionState` et les afficherait comme une
        // panne de serveur — un bug de l'interface déguisé en problème réseau,
        // qui envoie chercher au mauvais endroit.
        pairingFailed(null);
        return null;
      })
      .then(function (data) {
        if (!data) return;
        applySessionState(data);
        if (data.pairing) {
          $('tablet-url').textContent = data.pairing.url;
          $('tablet-qr').innerHTML = '';
          var image = new Image();
          image.src = '/api/session/qr.svg?v=' + encodeURIComponent(data.pairing.token);
          image.alt = 'QR code d’appairage';
          $('tablet-qr').appendChild(image);
          $('tablet-qr').style.display = '';
          $('tablet-nonet').style.display = 'none';
        } else if (sessionMode !== 'solo') {
          $('tablet-qr').style.display = 'none';
          $('tablet-nonet').style.display = '';
        }
      });
  }

  /** `arm` | `take` | `give` | `end` — voir app.api_session_tablet. */
  function sessionAction(action) {
    fetch('/api/session/tablet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action })
    })
      .then(function (response) {
        // Même garde que `refreshPairing` : sans elle, un refus du serveur
        // était traité comme une réussite et la suite s'exécutait dans le vide.
        if (!response.ok) { pairingFailed(response); return null; }
        return response.json();
      }, function () { pairingFailed(null); return null; })
      .then(function (data) {
        if (!data) return;
        if (action === 'arm') REMOTE.reopen();
        // Reprendre le stylet clôt les tracés que la tablette avait en cours :
        // sans cela ils resteraient des calques orphelins, visibles mais
        // jamais fusionnés ni exportés.
        if (action === 'take' || action === 'end') sealIncoming();
        if (action === 'arm') publishConfig();
        refreshPairing();
      })
      .catch(function () { status('Impossible de joindre le serveur.'); });
  }

  if (!isTablet) {
    $('btn-tablet').addEventListener('click', function () {
      $('tablet-screen').style.display = 'flex';
      if (sessionMode === 'solo') sessionAction('arm'); else refreshPairing();
    });
    $('tablet-close').addEventListener('click', function () {
      $('tablet-screen').style.display = 'none';
    });
    $('tablet-take').addEventListener('click', function () {
      $('tablet-screen').style.display = 'none';
      sessionAction('take');
    });
    $('tablet-give').addEventListener('click', function () {
      $('tablet-screen').style.display = 'none';
      sessionAction('give');
    });
    $('tablet-end').addEventListener('click', function () {
      if (!window.confirm('Terminer la session va perdre le projet non exporté. Continuer ?')) return;
      $('tablet-screen').style.display = 'none';
      sessionAction('end');
    });
    $('viewer-takeover').addEventListener('click', function () { sessionAction('take'); });
  }

  /* ------------------------------------------------ Gestion des proxys */

  /* Les copies de lecture survivent volontairement à la fermeture de
   * l'application : refaire plusieurs minutes de transcodage parce qu'on a
   * fermé la fenêtre serait absurde. En contrepartie, le ménage est explicite
   * et se fait ici. */

  function formatSize(bytes) {
    if (!bytes) return '0 Mo';
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' ko';
    var mo = bytes / (1024 * 1024);
    return mo >= 1024 ? (mo / 1024).toFixed(2) + ' Go' : mo.toFixed(1) + ' Mo';
  }

  function formatDate(seconds) {
    var date = new Date(seconds * 1000);
    return date.toLocaleDateString() + ' ' + pad2(date.getHours()) + ':' + pad2(date.getMinutes());
  }

  function refreshProxies() {
    return fetch('/api/proxies')
      .then(function (response) { return response.json(); })
      .then(renderProxies)
      .catch(function () { status('Cache des proxys illisible.'); });
  }

  function renderProxies(data) {
    var list = $('proxy-list');
    list.innerHTML = '';
    $('proxy-total').textContent = data.entries.length
      ? data.entries.length + ' fichier(s) — ' + formatSize(data.total)
      : '';
    $('proxy-empty').style.display = data.entries.length ? 'none' : '';
    $('proxy-purge').disabled = !data.entries.length;
    // Le bouton dit ce qui va se passer, et l'attente n'est pas la même : avec
    // une copie de lecture déjà en cache, c'est instantané — sans, c'est
    // plusieurs minutes de transcodage. Annoncer « transcoder » quand il n'y a
    // qu'à reprendre un fichier existant faisait craindre le pire, et laissait
    // croire qu'on refabriquait ce qu'on avait déjà.
    var reprise = data.current.hasProxy && !data.current.proxied;
    $('proxy-force').textContent = reprise ? 'Servir le proxy' : 'Transcoder ce rush';
    $('proxy-force').title = reprise
      ? 'Reprend la copie de lecture déjà en cache pour ce rush — immédiat'
      : 'Fabrique une copie lisible du rush affiché, même s’il semblait déjà bon '
        + '— à utiliser si l’aperçu reste noir';
    $('proxy-force').disabled = !data.current.hasSource || data.current.proxied;
    $('proxy-bypass').disabled = !data.current.hasSource || !data.current.proxied;
    setMediaSourceBadge(data.current.hasSource ? data.current.proxied : null);

    data.entries.forEach(function (entry) {
      var row = document.createElement('div');
      row.className = 'proxy-item' + (entry.inUse ? ' in-use' : '');

      var info = document.createElement('div');
      info.className = 'proxy-info';
      var name = document.createElement('span');
      name.className = 'proxy-name';
      name.textContent = entry.name;
      var meta = document.createElement('span');
      meta.className = 'proxy-meta';
      meta.textContent = formatDate(entry.created)
        + (entry.reason ? ' · ' + entry.reason : '');
      meta.title = entry.source || entry.file;
      info.appendChild(name);
      info.appendChild(meta);

      var size = document.createElement('span');
      size.className = 'proxy-size';
      size.textContent = formatSize(entry.size);

      var remove = document.createElement('button');
      remove.className = 'remove';
      remove.title = entry.inUse
        ? 'Supprimer ce proxy — le média affiché sera libéré'
        : 'Supprimer ce proxy';
      remove.textContent = '✕';
      remove.addEventListener('click', function () { deleteProxy(entry); });

      row.appendChild(info);
      row.appendChild(size);
      row.appendChild(remove);
      list.appendChild(row);
    });
  }

  function deleteProxy(entry) {
    if (entry.inUse && !window.confirm('« ' + entry.name + ' » est le média actuellement '
      + 'affiché.\n\nLe supprimer libérera l’image à l’écran. Continuer ?')) return;
    fetch('/api/proxies/' + encodeURIComponent(entry.file), { method: 'DELETE' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.error) { status(data.error); return; }
        status('Proxy « ' + entry.name + ' » supprimé.');
        refreshProxies();
      })
      .catch(function () { status('Suppression impossible.'); });
  }

  $('btn-proxies').addEventListener('click', function () {
    var panel = $('proxy-panel');
    var opening = panel.style.display !== 'flex';
    panel.style.display = opening ? 'flex' : 'none';
    if (opening) refreshProxies();
  });

  function setHelpOpen(open) {
    $('help-screen').style.display = open ? 'flex' : 'none';
  }

  $('btn-shortcuts').addEventListener('click', function () {
    setHelpOpen(true);
  });

  $('help-close').addEventListener('click', function () {
    setHelpOpen(false);
  });

  $('help-screen').addEventListener('click', function (event) {
    if (event.target === $('help-screen')) setHelpOpen(false);
  });

  $('proxy-close').addEventListener('click', function () {
    $('proxy-panel').style.display = 'none';
  });

  $('proxy-purge').addEventListener('click', function () {
    if (!window.confirm('Supprimer toutes les copies de lecture en cache ?\n\n'
      + 'Les rushs d’origine ne sont pas touchés, mais rouvrir l’un d’eux '
      + 'imposera un nouveau transcodage.')) return;
    fetch('/api/proxies', { method: 'DELETE' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        status(data.removed + ' proxy(s) supprimé(s)'
          + (data.kept ? ', ' + data.kept + ' encore verrouillé(s) par le lecteur.' : '.'));
        refreshProxies();
      })
      .catch(function () { status('Suppression impossible.'); });
  });

  $('proxy-bypass').addEventListener('click', function () {
    // Le repli automatique retranscoderait aussitôt sur un simple `error` du
    // lecteur : `bypassed` remonte du serveur et le fait taire pour ce rush.
    status('Lecture du rush d’origine, sans copie de lecture…');
    fetch('/api/proxies/bypass', { method: 'POST' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.error) { status(data.error); return; }
        refreshProxies();
      })
      .catch(function () { status('Impossible de servir l’original.'); });
  });

  $('proxy-force').addEventListener('click', function () {
    status('Transcodage du rush en cours…');
    fetch('/api/proxies/force', { method: 'POST' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.error) { status(data.error); return; }
        refreshProxies();
      })
      .catch(function () { status('Transcodage impossible.'); });
  });

  /* ---------------------------------------------------------- Démarrage */

  REMOTE.init({
    state: applySessionState,
    message: handleRemote,
    status: function (info) {
      if (isTablet) {
        updateTabletBadge();
        if (info.state === 'denied' || info.state === 'evicted' || info.state === 'ended') {
          showSessionOver(info.detail);
        }
      } else {
        updateViewerBadge();
        if (info.state === 'offline') sealIncoming();
      }
    }
  });

  if (isTablet) {
    document.body.classList.add('tablet');
    initTabletInterface();
    // Rien à configurer ici : le format de travail vient du poste.
    $('setup-screen').style.display = 'none';
    status('Connexion au poste…');
    REMOTE.connect();
    watchTransportForRemote();
    setInterval(updateTabletBadge, 1000);
  } else {
    // Le poste reste connecté même en solo : c'est par ce canal qu'arrivent
    // l'avancement du transcodage et l'état du média, mode tablette ou non.
    REMOTE.connect();
    watchTransportForRemote();
    refreshPairing();
    setInterval(function () { if (sessionMode !== 'solo') updateViewerBadge(); }, 1000);
  }

  buildPanel();
  loadLibrary();

  // Le fond retrouvé d'une session à l'autre : la modale de setup doit déjà
  // afficher le bon choix avant que l'espace de travail existe.
  config.background = readStoredBackground() || config.background;
  syncBackgroundControls();

  // Dossier proposé par défaut : celui du serveur, tant que rien n'est choisi.
  // Réservé au poste (l'export ne se déclenche pas depuis la tablette).
  if (!isTablet) {
    fetch('/api/export/defaults')
      .then(function (response) { return response.json(); })
      .then(function (data) { exportDirectory = data.directory || ''; })
      .catch(function () { /* le champ reste vide : le serveur retombera sur exports/ */ });
  }
})();
