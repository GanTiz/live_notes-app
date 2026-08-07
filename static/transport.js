/* live_notes — lecteur média maison.
 *
 * Les contrôles natifs du navigateur sont masques : ils sont non stylables,
 * n'affichent pas de timecode et ne savent pas gerer des points IN/OUT. Ce
 * module pilote entierement l'element <video>/<audio> de fond et expose les
 * points IN/OUT au reste de l'application.
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  var node = null;         /* <video> ou <audio> courant */
  var kind = null;         /* 'video' | 'audio' | 'image' */
  var fps = 30;
  var inPoint = 0;
  var outPoint = 0;
  var duration = 0;
  var locked = false;      /* verrouille pendant le REC */
  var raf = null;
  var dragging = null;     /* 'scrub' | 'in' | 'out' */
  var onRangeChange = null;
  var wired = null;        /* element deja muni de ses ecouteurs */

  /* ------------------------------------------------------------ Timecode */

  function pad(value, size) {
    var text = String(Math.abs(Math.floor(value)));
    while (text.length < (size || 2)) text = '0' + text;
    return text;
  }

  /** Secondes -> HH:MM:SS:FF (non drop-frame). */
  function toTimecode(seconds) {
    if (!isFinite(seconds) || seconds < 0) seconds = 0;
    var totalFrames = Math.round(seconds * fps);
    var frames = totalFrames % fps;
    var totalSeconds = Math.floor(totalFrames / fps);
    return pad(Math.floor(totalSeconds / 3600)) + ':'
      + pad(Math.floor(totalSeconds / 60) % 60) + ':'
      + pad(totalSeconds % 60) + ':'
      + pad(frames);
  }

  /** HH:MM:SS:FF, MM:SS:FF, SS ou 12.5 -> secondes. null si illisible. */
  function fromTimecode(text) {
    if (text == null) return null;
    var trimmed = String(text).trim();
    if (!trimmed) return null;

    if (trimmed.indexOf(':') < 0) {
      var plain = parseFloat(trimmed.replace(',', '.'));
      return isFinite(plain) ? plain : null;
    }

    var parts = trimmed.split(':');
    if (parts.length > 4) return null;
    var numbers = [];
    for (var i = 0; i < parts.length; i++) {
      var value = parseInt(parts[i], 10);
      if (!isFinite(value) || value < 0) return null;
      numbers.push(value);
    }
    // Le dernier champ est toujours des images ; on complete a gauche.
    while (numbers.length < 4) numbers.unshift(0);
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2] + numbers[3] / fps;
  }

  function clamp(value, lo, hi) { return value < lo ? lo : (value > hi ? hi : value); }

  /** Une image, exprimee en secondes : le pas minimal de tout le lecteur. */
  function frameStep() { return 1 / fps; }

  /* Cale un temps sur la grille d'images.
   *
   * Une borne designe une image, pas un instant quelconque entre deux. Posee a
   * la souris elle tombait au milieu d'une image, et chacun l'arrondissait de
   * son cote -- le poste, la tablette, puis le moteur de rendu. Une image
   * d'ecart suffit a ce que le trace ne retombe plus en face du rush. On cale
   * donc ici, une fois pour toutes, et tout le monde parle des memes images. */
  function snap(seconds) {
    return Math.round(seconds * fps) / fps;
  }

  /* Derniere position de lecture encore dans la plage.
   *
   * Le *milieu* de la derniere image, et non son debut : aucun arrondi ne peut
   * alors la faire deborder sur la suivante, qui est hors plage. C'est la que
   * le lecteur se gare quand il atteint le point OUT, pour que l'image laissee
   * a l'ecran soit la derniere de ce qui a ete annote. */
  function lastFrameTime() {
    return Math.max(inPoint, outPoint - frameStep() / 2);
  }

  /* -------------------------------------------------------------- Rendu */

  function ratio(seconds) {
    return duration > 0 ? clamp(seconds / duration, 0, 1) * 100 : 0;
  }

  function paint() {
    if (!node || kind === 'image') return;
    var current = node.currentTime || 0;

    $('tp-current').textContent = toTimecode(current);
    $('tp-duration').textContent = toTimecode(duration);
    $('tp-playhead').style.left = ratio(current) + '%';
    $('tp-progress').style.left = ratio(inPoint) + '%';
    $('tp-progress').style.width = Math.max(0, ratio(current) - ratio(inPoint)) + '%';
    $('tp-range').style.left = ratio(inPoint) + '%';
    $('tp-range').style.width = Math.max(0, ratio(outPoint) - ratio(inPoint)) + '%';
    $('tp-in').style.left = ratio(inPoint) + '%';
    $('tp-out').style.left = ratio(outPoint) + '%';
    $('tp-play').textContent = node.paused ? '▶' : '❚❚';

    if (document.activeElement !== $('tp-in-tc')) $('tp-in-tc').value = toTimecode(inPoint);
    if (document.activeElement !== $('tp-out-tc')) $('tp-out-tc').value = toTimecode(outPoint);
  }

  function tick() {
    if (!node) return;
    // Hors enregistrement, la lecture reste bornee par les points IN/OUT. On
    // s'arrete des que la derniere image de la plage a ete montree -- une demi
    // image avant OUT, la ou elle est encore a l'ecran -- et l'on se gare sur
    // elle. Se garer sur OUT lui-meme figeait la premiere image *hors* plage.
    if (!locked && !node.paused && node.currentTime >= outPoint - frameStep() / 2) {
      node.pause();
      seek(lastFrameTime());
    }
    paint();
    raf = requestAnimationFrame(tick);
  }

  function startLoop() {
    if (raf === null) raf = requestAnimationFrame(tick);
  }

  function stopLoop() {
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null;
  }

  /* ---------------------------------------------------------- Commandes */

  function seek(seconds) {
    if (!node) return;
    node.currentTime = clamp(seconds, 0, duration);
    paint();
  }

  function togglePlay() {
    if (!node || locked) return;
    if (node.paused) {
      if (node.currentTime < inPoint || node.currentTime >= outPoint - frameStep()) seek(inPoint);
      node.play().catch(function () { /* lecture refusee : sans conséquence ici */ });
    } else {
      node.pause();
    }
    paint();
  }

  function nudge(seconds) {
    if (!node || locked) return;
    seek(node.currentTime + seconds);
  }

  /** Avance image par image : on se recale sur la grille d'images. */
  function stepFrame(direction) {
    if (!node || locked) return;
    if (!node.paused) node.pause();
    seek((Math.round(node.currentTime * fps) + direction) / fps);
  }

  /* Rendre la main sans rien changer quand rien ne change : c'est ce qui
   * empêche la synchronisation des bornes entre les deux écrans de reboucler
   * (l'un applique ce qu'il reçoit, ce qui le ferait réémettre, etc.). */
  function setRange(newIn, newOut) {
    var minGap = frameStep();
    var nextIn, nextOut;
    if (duration > 0) {
      nextIn = clamp(snap(newIn), 0, Math.max(0, duration - minGap));
      nextOut = clamp(snap(newOut), nextIn + minGap, duration);
    } else {
      /* Duree encore inconnue -- le media vient d'etre installe et ses
       * metadonnees ne sont pas arrivees. L'ecretage contre zero ramenait
       * alors *toute* plage a rien, et l'annoncait : les bornes qu'un ecran
       * reposait a ce moment-la se retournaient en « plage vide » chez
       * l'autre. On retient ce qui est demande ; `settleRange` le bornera
       * contre le media des qu'il aura une duree a opposer. */
      nextIn = Math.max(0, snap(newIn));
      nextOut = Math.max(nextIn + minGap, snap(newOut));
    }
    // Comparaison après bornage : deux valeurs différentes peuvent se ramener
    // aux mêmes bornes, et cela ne fait pas un changement.
    if (nextIn === inPoint && nextOut === outPoint) return;
    inPoint = nextIn;
    outPoint = nextOut;
    paint();
    if (onRangeChange) onRangeChange(inPoint, outPoint);
  }

  /* Pose les bornes une fois la duree connue, sans prevenir personne.
   *
   * `attach` ne decide de rien : il reinstalle des bornes deja posees, ou
   * ouvre la plage entiere d'un rush qui arrive. Annoncer cela comme un
   * changement faisait ecraser les bornes de l'autre ecran par un lecteur qui
   * venait a peine de naitre -- une tablette qui se connectait effacait celles
   * du poste. C'est a l'appelant, qui sait de quel cas il s'agit, de publier.
   *
   * Des bornes qui depassent le media sont ramenees dedans : un rush plus
   * court que le precedent ne garde pas une sortie au-dela de sa fin. */
  function settleRange() {
    if (!duration) return;
    var minGap = frameStep();
    var start = clamp(snap(inPoint), 0, Math.max(0, duration - minGap));
    var end = outPoint > start
      ? clamp(snap(outPoint), start + minGap, duration)
      : duration;
    inPoint = start;
    outPoint = end;
    paint();
  }

  /* ------------------------------------------------- Barre de progression */

  function positionToTime(clientX) {
    var rect = $('tp-track').getBoundingClientRect();
    if (!rect.width) return 0;
    return clamp((clientX - rect.left) / rect.width, 0, 1) * duration;
  }

  function onTrackPointerDown(event) {
    if (!node || locked || kind === 'image') return;
    var target = event.target;

    if (target.id === 'tp-in') dragging = 'in';
    else if (target.id === 'tp-out') dragging = 'out';
    else dragging = 'scrub';

    if (dragging !== 'scrub') target.classList.add('dragging');
    $('tp-track').setPointerCapture(event.pointerId);
    onTrackPointerMove(event);
  }

  function onTrackPointerMove(event) {
    if (!dragging) return;
    event.preventDefault();
    var time = positionToTime(event.clientX);
    if (dragging === 'in') setRange(time, outPoint);
    else if (dragging === 'out') setRange(inPoint, time);
    else seek(time);
  }

  function onTrackPointerUp(event) {
    if (!dragging) return;
    dragging = null;
    $('tp-in').classList.remove('dragging');
    $('tp-out').classList.remove('dragging');
    try { $('tp-track').releasePointerCapture(event.pointerId); } catch (e) { /* deja relache */ }
  }

  /* ------------------------------------------------------- Branchements */

  function bindOnce() {
    $('tp-play').addEventListener('click', togglePlay);
    $('tp-back').addEventListener('click', function () { nudge(-10); });
    $('tp-fwd').addEventListener('click', function () { nudge(10); });
    $('tp-frame-back').addEventListener('click', function () { stepFrame(-1); });
    $('tp-frame-fwd').addEventListener('click', function () { stepFrame(1); });

    $('tp-track').addEventListener('pointerdown', onTrackPointerDown);
    $('tp-track').addEventListener('pointermove', onTrackPointerMove);
    $('tp-track').addEventListener('pointerup', onTrackPointerUp);
    $('tp-track').addEventListener('pointercancel', onTrackPointerUp);

    $('tp-in-set').addEventListener('click', function () {
      if (node && !locked) setRange(node.currentTime, outPoint);
    });
    $('tp-out-set').addEventListener('click', function () {
      if (node && !locked) setRange(inPoint, node.currentTime);
    });
    $('tp-reset').addEventListener('click', function () {
      if (node && !locked) setRange(0, duration);
    });

    commitOnEnter($('tp-in-tc'), function (seconds) { setRange(seconds, outPoint); });
    commitOnEnter($('tp-out-tc'), function (seconds) { setRange(inPoint, seconds); });

    $('tp-toggle').addEventListener('click', function () {
      $('transport').classList.toggle('collapsed');
      try {
        localStorage.setItem('live_notes.transportCollapsed',
          $('transport').classList.contains('collapsed') ? '1' : '0');
      } catch (e) { /* quota */ }
    });

    if (readCollapsed()) $('transport').classList.add('collapsed');
  }

  function readCollapsed() {
    try { return localStorage.getItem('live_notes.transportCollapsed') === '1'; }
    catch (e) { return false; }
  }

  function commitOnEnter(input, apply) {
    function commit() {
      var seconds = fromTimecode(input.value);
      if (seconds === null || locked) { paint(); return; }
      apply(seconds);
    }
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { commit(); input.blur(); }
      if (event.key === 'Escape') { paint(); input.blur(); }
    });
  }

  /* ---------------------------------------------------------------- API */

  var Transport = {
    /** Prend la main sur un media de fond. `media` peut etre une <img>.
     *  Rappelable a volonte : changer de media repart d'un etat neuf. */
    attach: function (media, options) {
      node = media;
      kind = options.kind;
      fps = options.fps || 30;
      onRangeChange = options.onRangeChange || null;
      // Bornes conservées quand l'appelant en fournit (bascule proxy ↔ original,
      // refonte de l'espace de travail, projet rechargé) ; plage entière sinon.
      // Elles sont posées telles quelles, sans passer par `setRange` : personne
      // n'a rien décidé ici, il n'y a donc rien à annoncer.
      inPoint = typeof options.inPoint === 'number' && isFinite(options.inPoint)
        ? options.inPoint : 0;
      outPoint = typeof options.outPoint === 'number' && isFinite(options.outPoint)
        ? options.outPoint : 0;
      duration = 0;
      dragging = null;
      Transport.setLocked(false);

      var bar = $('transport');
      bar.classList.remove('no-media');
      $('tp-choose').textContent = 'Changer de média…';
      $('tp-name').textContent = options.name || '';

      var timed = kind !== 'image';
      // Une image n'a ni duree ni lecture : seule l'opacite du fond reste utile.
      $('tp-track').parentNode.style.display = timed ? '' : 'none';
      ['tp-in-tc', 'tp-out-tc', 'tp-in-set', 'tp-out-set', 'tp-reset'].forEach(function (id) {
        $(id).style.display = timed ? '' : 'none';
      });
      Array.prototype.forEach.call(document.querySelectorAll('.tp-row.sub label'), function (label) {
        label.style.display = timed ? '' : 'none';
      });

      if (!timed) { stopLoop(); return; }

      node.controls = false;
      // `attach` est rappelable sur le *meme* element : refaire l'espace de
      // travail change la cadence sans changer de rush. Les ecouteurs, eux, ne
      // se posent qu'une fois -- empiles, chacun rejouerait a l'arrivee des
      // metadonnees les bornes qu'il avait capturees, et la derniere valeur
      // posee serait la plus ancienne.
      if (wired !== node) {
        wired = node;
        node.addEventListener('loadedmetadata', function () {
          duration = isFinite(node.duration) ? node.duration : 0;
          settleRange();
        });
        node.addEventListener('play', paint);
        node.addEventListener('pause', paint);
        node.addEventListener('seeked', paint);
      }

      duration = isFinite(node.duration) ? node.duration : 0;
      settleRange();
      startLoop();
      paint();
    },

    /** Plus aucun media de fond : le media a ete retire a la main, remplace par
     *  un autre rush, ou son proxy vient d'etre efface au lancement de l'export.
     *  L'en-tete revient a son etat d'origine plutot que d'afficher un timecode
     *  qui ne correspond plus a rien. Les ecouteurs poses par `attach` partent
     *  avec l'element, qui est jete. */
    detach: function () {
      stopLoop();
      node = null;
      wired = null;
      kind = null;
      inPoint = 0;
      outPoint = 0;
      duration = 0;
      dragging = null;
      Transport.setLocked(false);
      $('transport').classList.add('no-media');
      $('tp-choose').textContent = 'Choisir un média…';
      $('tp-name').textContent = '';
    },

    inPoint: function () { return inPoint; },
    outPoint: function () { return outPoint > inPoint ? outPoint : (duration || Infinity); },

    /** Pose les bornes depuis l'extérieur — l'autre écran, ou la session
     *  republiée à l'arrivée d'une tablette. Passe par le même chemin que les
     *  poignées, donc par la même garde anti-rebouclage. */
    setRange: setRange,
    duration: function () { return duration; },
    toTimecode: toTimecode,

    /** Une image, en secondes, a la cadence du transport. */
    frameStep: frameStep,

    /** Ramene la tete de lecture sur la derniere image de la plage.
     *
     *  Un enregistrement laisse le rush la ou le decodeur l'a mene, souvent
     *  une ou deux images au-dela du point OUT -- davantage sur une tablette,
     *  qui recoit son rush par le reseau. L'image figee a l'ecran n'etait alors
     *  plus celle qu'on venait d'annoter. */
    parkAtOut: function () {
      if (!node || kind === 'image') return;
      seek(lastFrameTime());
    },

    /** Pendant le REC, l'utilisateur ne doit plus pouvoir toucher au transport. */
    setLocked: function (value) {
      locked = !!value;
      $('transport').classList.toggle('locked', locked);
      ['tp-play', 'tp-back', 'tp-fwd', 'tp-frame-back', 'tp-frame-fwd', 'tp-choose',
       'tp-clear', 'tp-in-set', 'tp-out-set', 'tp-reset', 'tp-in-tc', 'tp-out-tc']
        .forEach(function (id) { $(id).disabled = locked; });
      $('tp-track').classList.toggle('locked', locked);
      paint();
    },

    togglePlay: togglePlay,
    isTimed: function () { return !!node && kind !== 'image'; }
  };

  bindOnce();
  window.MediaTransport = Transport;
})();
