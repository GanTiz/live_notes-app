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

  /** Secondes -> HH:MM:SS:FF (non drop-frame).
   *
   *  Le timecode d'un instant est celui de l'image qui est *a l'ecran* a cet
   *  instant, donc celle dont l'intervalle le contient : une division entiere.
   *  L'arrondi a l'image la plus proche, lui, changeait de reponse au milieu
   *  d'une image -- l'ecran montrait la 19 et le compteur annoncait la 20.
   *  C'est cette contradiction que l'on prenait pour une borne « instable ». */
  function toTimecode(seconds) {
    if (!isFinite(seconds) || seconds < 0) seconds = 0;
    var totalFrames = frameOf(seconds);
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

  /* Marge de calcul, tres en dessous d'une image et tres au-dessus du bruit du
   * calcul flottant : 29/25*25 vaut 28.999999999999996, et sans elle la 29e
   * image se lirait comme la 28e. */
  var EPS = 1e-6;

  /** Numero de l'image qui occupe cet instant. L'image `n` va de `n/fps`
   *  (inclus) a `(n+1)/fps` (exclu) : une division entiere, jamais un arrondi. */
  function frameOf(seconds) {
    return Math.floor(Math.max(0, seconds) * fps + EPS);
  }

  /** Debut de l'image qui occupe cet instant. */
  function frameStart(seconds) { return frameOf(seconds) / fps; }

  /** Fin de l'image qui occupe cet instant, c.-a-d. debut de la suivante. */
  function frameEnd(seconds) { return (frameOf(seconds) + 1) / fps; }

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

  /* Numero de la derniere image de la plage.
   *
   * `outPoint` est la *fin* de la plage : l'instant ou commence la premiere
   * image qui n'en fait plus partie. C'est ce dont le moteur de rendu a besoin
   * (`-to`), et c'est la duree de la plage qui s'en deduit sans correction.
   *
   * Ce que l'utilisateur.ice designe, en revanche, est une image : au montage,
   * le point de sortie se pose sur la *derniere image du plan*, pas sur la
   * premiere du plan suivant. Les deux se traduisent l'un dans l'autre ici, et
   * nulle part ailleurs -- `outFrame()` pour l'afficher et le poser,
   * `outPoint` pour tout le reste. */
  function outFrame() {
    return Math.max(frameOf(inPoint), Math.ceil(outPoint * fps - EPS) - 1);
  }

  /* Derniere position de lecture encore dans la plage.
   *
   * Le *milieu* de la derniere image, et non l'une de ses bornes : aucun
   * arrondi, ni cote navigateur ni ici, ne peut alors la faire basculer sur sa
   * voisine. C'est la que le lecteur se gare quand il atteint le point OUT,
   * pour que l'image laissee a l'ecran soit la derniere de ce qui a ete
   * annote. */
  function lastFrameTime() {
    return (outFrame() + 0.5) / fps;
  }

  /* La derniere image de la plage est-elle deja a l'ecran ?
   *
   * `at` est l'instant de l'image *presentee* quand on le sait
   * (`requestVideoFrameCallback`), l'horloge du lecteur a defaut. La nuance
   * decide de tout : voir plus bas `watchOut`. */
  function reachedOut(at) {
    return frameOf(at) >= outFrame();
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
    // Le champ redit l'image designee, pas la fin de la plage : ce que l'on
    // tape et ce que l'on relit doivent etre le meme timecode.
    if (document.activeElement !== $('tp-out-tc')) {
      $('tp-out-tc').value = toTimecode(outFrame() / fps);
    }
  }

  function tick() {
    if (!node) return;
    // Hors enregistrement, la lecture reste bornee par les points IN/OUT. Le
    // guet a l'image presentee (`watchOut`) fait l'essentiel ; celui-ci reste
    // en second filet pour les medias qui n'ont pas d'image a presenter -- un
    // son -- et pour un lecteur dont le rappel d'image ne partirait pas.
    if (!locked && !node.paused && reachedOut(node.currentTime || 0)) haltAtOut();
    paint();
    raf = requestAnimationFrame(tick);
  }

  /* Arret sur la derniere image de la plage, sans jamais montrer la suivante.
   *
   * L'horloge du lecteur retarde sur ce que le compositeur affiche : elle est
   * mise a jour a son propre rythme, et un decodeur qui rattrape un a-coup de
   * reseau -- le quotidien d'une tablette -- presente plusieurs images avant
   * que `currentTime` ne l'admette. Guetter la fin de plage a cette horloge-la,
   * c'est s'arreter apres coup : l'image d'apres le point OUT passait a
   * l'ecran, une image durant, avant le retour en arriere. C'etait l'image
   * fantome, et aucune marge prise sur l'horloge ne pouvait la supprimer --
   * elle mesurait la mauvaise chose.
   *
   * `requestVideoFrameCallback` dit *quelle* image vient d'etre presentee, au
   * moment ou elle l'est. Des que c'est celle du point OUT, il reste une duree
   * d'image entiere pour arreter le lecteur : la suivante n'est jamais
   * composee. C'est le seul signal en avance sur elle. */
  function watchOut(onReached) {
    if (!node || kind === 'image'
      || typeof node.requestVideoFrameCallback !== 'function') {
      return function () { };
    }
    var watched = node;
    var stopped = false;
    var handle = null;

    function presented(now, metadata) {
      if (stopped || watched !== node) return;
      var at = metadata && typeof metadata.mediaTime === 'number'
        ? metadata.mediaTime : (watched.currentTime || 0);
      if (reachedOut(at)) { stopped = true; onReached(); return; }
      handle = watched.requestVideoFrameCallback(presented);
    }

    handle = watched.requestVideoFrameCallback(presented);
    return function () {
      stopped = true;
      if (handle !== null && watched.cancelVideoFrameCallback) {
        try { watched.cancelVideoFrameCallback(handle); } catch (error) { /* deja parti */ }
      }
    };
  }

  /** Fin de plage atteinte hors enregistrement : on s'arrete et on se gare. */
  function haltAtOut() {
    if (!node || node.paused) return;
    node.pause();
    parkAtOut();
  }

  /* Ramene la tete de lecture sur la derniere image de la plage.
   *
   * Sans rien faire quand elle y est deja : un `seek` redecode, et cette
   * secousse-la se voit autant que celle qu'on voulait corriger. */
  function parkAtOut() {
    if (!node || kind === 'image') return;
    if (frameOf(node.currentTime || 0) === outFrame()) { paint(); return; }
    seek(lastFrameTime());
  }

  var disarm = null;

  function armOutWatch() {
    disarmOutWatch();
    if (locked) return;   // pendant le REC, c'est l'enregistrement qui borne
    disarm = watchOut(haltAtOut);
  }

  function disarmOutWatch() {
    if (disarm) { disarm(); disarm = null; }
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

  /** Se poser sur une image donnee, en son milieu -- voir `lastFrameTime`. */
  function seekFrame(index) {
    seek((Math.max(0, index) + 0.5) / fps);
  }

  function togglePlay() {
    if (!node || locked) return;
    if (node.paused) {
      var at = frameOf(node.currentTime || 0);
      if (at < frameOf(inPoint) || at >= outFrame()) seekFrame(frameOf(inPoint));
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
    seekFrame(frameOf(node.currentTime || 0) + direction);
  }

  /* Pose la plage, en coordonnees de plage : `newIn` est son debut, `newOut`
   * sa fin -- l'instant ou commence la premiere image qui n'en fait plus
   * partie. C'est le contrat que se partagent les deux ecrans, la session et le
   * moteur de rendu ; la traduction depuis l'image designee se fait une seule
   * fois, dans `setInAt` / `setOutAt`. Le rappeler avec ce qu'il vient de
   * rendre ne doit rien deplacer.
   *
   * Rendre la main sans rien changer quand rien ne change : c'est ce qui
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
    if (dragging === 'in') setInAt(time);
    else if (dragging === 'out') setOutAt(time);
    else seek(time);
  }

  /* Les deux seuls chemins par lesquels une personne designe une image.
   *
   * Le point IN est l'image ou l'on entre : sa borne est son debut. Le point
   * OUT est la derniere image gardee : la plage court donc jusqu'a sa *fin*.
   * Poser la borne sur le debut de l'image designee revenait a l'exclure --
   * c'est l'image de trop, ou de moins, qu'on se disputait de bout en bout de
   * la chaine. */
  function setInAt(seconds) { setRange(frameStart(seconds), outPoint); }
  function setOutAt(seconds) { setRange(inPoint, frameEnd(seconds)); }

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
      if (node && !locked) setInAt(node.currentTime);
    });
    $('tp-out-set').addEventListener('click', function () {
      if (node && !locked) setOutAt(node.currentTime);
    });
    $('tp-reset').addEventListener('click', function () {
      if (node && !locked) setRange(0, duration);
    });

    commitOnEnter($('tp-in-tc'), setInAt);
    commitOnEnter($('tp-out-tc'), setOutAt);

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
        // L'element est retenu ici, et non relu dans `node` : les ecouteurs
        // restent poses sur lui apres un `detach`, qui met `node` a null. Un
        // `loadedmetadata` arrive en retard -- le rush qu'on vient de lacher
        // finit de charger -- lisait alors la duree de rien.
        var wiredNode = node;
        wired = node;
        var live = function () { return node === wiredNode; };
        wiredNode.addEventListener('loadedmetadata', function () {
          if (!live()) return;
          duration = isFinite(wiredNode.duration) ? wiredNode.duration : 0;
          settleRange();
        });
        wiredNode.addEventListener('play', function () {
          if (!live()) return;
          armOutWatch(); paint();
        });
        wiredNode.addEventListener('pause', function () {
          if (!live()) return;
          disarmOutWatch(); paint();
        });
        wiredNode.addEventListener('seeked', function () { if (live()) paint(); });
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
      disarmOutWatch();
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
    /** Fin de la plage : l'instant ou commence la premiere image qui n'en fait
     *  plus partie. La duree de la plage en decoule sans correction, et c'est
     *  ce que `-to` attend du cote du moteur de rendu. */
    outPoint: function () { return outPoint > inPoint ? outPoint : (duration || Infinity); },

    /** Pose les bornes depuis l'extérieur — l'autre écran, ou la session
     *  republiée à l'arrivée d'une tablette. Passe par le même chemin que les
     *  poignées, donc par la même garde anti-rebouclage. */
    setRange: setRange,
    duration: function () { return duration; },
    toTimecode: toTimecode,

    /** Une image, en secondes, a la cadence du transport. */
    frameStep: frameStep,

    /** Numero de l'image qui occupe cet instant, et celui des deux bornes. */
    frameOf: frameOf,
    outFrame: outFrame,

    /** Milieu de la premiere image de la plage : c'est de la que part une
     *  lecture, plutot que de la frontiere exacte ou le decodeur peut retomber
     *  d'un cote comme de l'autre. */
    firstFrameTime: function () { return (frameOf(inPoint) + 0.5) / fps; },

    /** L'image du point OUT est-elle atteinte ? `at` est l'instant de l'image
     *  presentee quand on le connait, l'horloge du lecteur a defaut. */
    reachedOut: reachedOut,

    /** Previent des que l'image du point OUT est presentee, et rend de quoi
     *  desarmer. Voir le commentaire de `watchOut` : c'est le seul signal qui
     *  arrive avant l'image suivante. */
    watchOut: watchOut,

    /** Ramene la tete de lecture sur la derniere image de la plage.
     *
     *  Un enregistrement laisse le rush la ou le decodeur l'a mene, souvent
     *  une ou deux images au-dela du point OUT -- davantage sur une tablette,
     *  qui recoit son rush par le reseau. L'image figee a l'ecran n'etait alors
     *  plus celle qu'on venait d'annoter. */
    parkAtOut: parkAtOut,

    /** Pendant le REC, l'utilisateur ne doit plus pouvoir toucher au transport. */
    setLocked: function (value) {
      locked = !!value;
      if (locked) disarmOutWatch();
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
