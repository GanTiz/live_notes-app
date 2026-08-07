/* live_notes — liaison temps reel entre le poste et la tablette.
 *
 * Ce module ne connait rien au dessin. Il transporte des messages, tient la
 * connexion ouverte, et redonne la main a app.js qui, lui, sait ce qu'un
 * « point de trace » veut dire. La separation est volontaire : la logique de
 * dessin reste unique et partagee par les deux roles, c'est ce qui garantit
 * que la tablette et le poste affichent exactement la meme chose.
 *
 * Ce qui circule, ce sont des gestes -- coordonnees, pression, pinceau,
 * horodatage -- jamais des pixels. Un trace complet de plusieurs secondes pese
 * quelques kilooctets ; le meme trace en video couterait des megabits et
 * ajouterait la latence d'un encodeur au bout du stylet.
 *
 * Le role se deduit du chemin : « /tablet » avec le jeton d'appairage dans le
 * fragment d'URL. Le fragment plutot que la query string parce qu'il n'est ni
 * envoye au serveur dans les requetes ordinaires, ni inscrit dans les journaux
 * d'acces.
 */
(function () {
  'use strict';

  var TABLET = window.location.pathname.replace(/\/+$/, '') === '/tablet';
  var role = TABLET ? 'tablet' : 'pc';
  var token = TABLET ? decodeURIComponent(window.location.hash.replace(/^#/, '')) : '';

  var socket = null;
  var open = false;
  var closedForGood = false;   /* jeton refuse : inutile d'insister */
  var attempt = 0;
  var retryTimer = null;
  var pingTimer = null;

  var hooks = { state: null, message: null, status: null };
  var latency = null;

  /* Motif d'arret definitif (jeton perime, poste ayant repris la main). Il
   * survit aux rafraichissements d'interface : sans cela, le prochain
   * battement de l'affichage remplacerait « session fermee par le poste » par
   * un banal « reconnexion… », et on perdrait la seule information utile. */
  var fatal = null;

  /* Surveillance du lien par pong.
   *
   * `WebSocket.send()` ne signale rien quand le reseau est tombe : le
   * navigateur empile les octets dans un tampon local et rend la main comme
   * si tout allait bien. Un lien mort peut donc rester « ouvert » plusieurs
   * dizaines de secondes cote JavaScript. Le pong est le seul temoin fiable :
   * sans reponse pendant PONG_TIMEOUT, on considere le lien perdu et on
   * relance la connexion nous-memes. */
  var lastPong = 0;
  var PONG_TIMEOUT = 8000;

  /* File d'attente de sortie pendant une coupure.
   *
   * Bornee, et volontairement bas : au-dela de quelques secondes de dessin,
   * rejouer l'arriere en rafale n'a plus de sens -- le poste afficherait un
   * trace qui se dessine tout seul en accelere. Mieux vaut perdre le milieu et
   * rester honnete sur ce qui a ete recu. */
  var pending = [];
  var PENDING_MAX = 600;

  function log(state, detail) {
    if (hooks.status) hooks.status({ state: state, detail: detail || '', latency: latency });
  }

  /* ------------------------------------------------------------ Connexion */

  function url() {
    var scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var base = scheme + '//' + window.location.host + '/ws?role=' + role;
    return token ? base + '&token=' + encodeURIComponent(token) : base;
  }

  function connect() {
    if (closedForGood || socket) return;
    try {
      socket = new WebSocket(url());
    } catch (error) {
      socket = null;
      scheduleRetry();
      return;
    }

    socket.onopen = function () {
      open = true;
      attempt = 0;
      lastPong = performance.now();
      log('online');
      flush();
      startPing();
    };

    socket.onmessage = function (event) {
      var message;
      try { message = JSON.parse(event.data); } catch (error) { return; }
      dispatch(message);
    };

    socket.onclose = function () {
      open = false;
      socket = null;
      stopPing();
      if (!closedForGood) {
        log('offline');
        scheduleRetry();
      }
    };

    // `onerror` precede toujours `onclose` : tout le travail de reprise est
    // fait la-bas, sinon on programmerait deux reconnexions concurrentes.
    socket.onerror = function () { };
  }

  function dispatch(message) {
    if (message.t === 'denied' || message.t === 'evicted' || message.t === 'ended') {
      // Fin de partie : on ferme pour de bon plutôt que de laisser une
      // connexion à moitié vivante répondre « en ligne » à l'interface.
      fatal = { state: message.t, detail: message.reason || '' };
      closedForGood = true;
      log(message.t, fatal.detail);
      shutdown();
      return;
    }
    if (message.t === 'pong') {
      lastPong = performance.now();
      if (typeof message.ts === 'number') latency = Math.round(performance.now() - message.ts);
      log('online');
      return;
    }
    if (message.t === 'state' && hooks.state) { hooks.state(message); return; }
    if (hooks.message) hooks.message(message);
  }

  function shutdown() {
    stopPing();
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    var doomed = socket;
    socket = null;
    open = false;
    if (doomed) {
      doomed.onclose = null;
      try { doomed.close(); } catch (error) { /* déjà fermé */ }
    }
  }

  /* Reprise progressive : un poste en veille ou un Wi-Fi qui bascule de borne
   * revient en quelques secondes, mais on ne martele pas le serveur pendant
   * une coupure longue. */
  function scheduleRetry() {
    if (retryTimer || closedForGood) return;
    attempt += 1;
    var delay = Math.min(500 * Math.pow(1.6, attempt - 1), 5000);
    retryTimer = setTimeout(function () {
      retryTimer = null;
      connect();
    }, delay);
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(function () {
      if (!open) return;
      if (performance.now() - lastPong > PONG_TIMEOUT) {
        // Le lien ne répond plus : on le déclare mort ici, sinon le navigateur
        // continuerait d'accepter des envois qui ne partiront jamais.
        latency = null;
        log('offline', 'Aucune réponse du serveur.');
        var doomed = socket;
        socket = null;
        open = false;
        if (doomed) { try { doomed.close(); } catch (error) { /* déjà fermé */ } }
        scheduleRetry();
        return;
      }
      raw(JSON.stringify({ t: 'ping', ts: performance.now() }));
    }, 2000);
  }

  function stopPing() {
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = null;
  }

  /* -------------------------------------------------------------- Envoi */

  function raw(text) {
    try { socket.send(text); return true; } catch (error) { return false; }
  }

  function send(message) {
    var text = JSON.stringify(message);
    if (open && raw(text)) return true;
    pending.push(text);
    if (pending.length > PENDING_MAX) pending.splice(0, pending.length - PENDING_MAX);
    return false;
  }

  function flush() {
    var queued = pending;
    pending = [];
    for (var i = 0; i < queued.length; i++) {
      if (!raw(queued[i])) { pending = queued.slice(i); return; }
    }
  }

  /* ---------------------------------------------------------------- API */

  window.LiveRemote = {
    role: role,
    isTablet: TABLET,
    token: token,

    /** hooks : { state, message, status } — voir app.js. */
    init: function (handlers) {
      hooks.state = handlers.state || null;
      hooks.message = handlers.message || null;
      hooks.status = handlers.status || null;
    },

    /** Le poste n'ouvre le canal que lorsque le mode tablette est arme :
     *  en solo, rien ne doit changer au comportement historique. */
    connect: connect,

    close: function () {
      closedForGood = true;
      shutdown();
      pending = [];
    },

    /** Rouvre un canal ferme par `close()` (retour en mode tablette). */
    reopen: function () {
      closedForGood = false;
      fatal = null;
      attempt = 0;
      connect();
    },

    send: send,
    isOnline: function () { return open; },
    latency: function () { return latency; },

    /** Motif d'arret definitif, ou null tant que la reprise reste possible. */
    fatal: function () { return fatal; }
  };
})();
