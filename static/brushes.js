/* Moteur de pinceaux — cote navigateur (preview live).
 *
 * Miroir exact de `brush_engine.py`. Meme format de pinceau (brushes.json),
 * meme RNG (mulberry32), meme repartition des empreintes le long du trace :
 * ce qu'on voit en dessinant est ce que Python recalculera a l'export.
 */
(function (global) {
  'use strict';

  var BASE_RES = 128;
  var DEG = Math.PI / 180;

  /* ---------------------------------------------------------------- RNG */

  function Rng(seed) {
    this.a = seed >>> 0;
  }

  Rng.prototype.next = function () {
    this.a = (this.a + 0x6d2b79f5) | 0;
    var a = this.a;
    var t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  Rng.prototype.sym = function () {
    return this.next() * 2 - 1;
  };

  /* ------------------------------------------------- Pinceaux / defauts */

  var DEFAULTS = {
    shape: 'round', color: '#111111', size: 16, opacity: 1, flow: 1,
    hardness: 0.9, spacing: 0.06, aspect: 1, angle: 0, follow: 0,
    eraser: false, texture: null,
    jitter: { position: 0, size: 0, angle: 0, opacity: 0 },
    pressure: { size: 0, opacity: 0 }
  };

  function setDefaults(defaults) {
    if (defaults) DEFAULTS = normalize(defaults);
  }

  function clamp(value, lo, hi) {
    return value < lo ? lo : (value > hi ? hi : value);
  }

  function normalize(brush) {
    var out = Object.assign({}, DEFAULTS, brush || {});
    out.jitter = Object.assign({}, DEFAULTS.jitter, (brush && brush.jitter) || {});
    out.pressure = Object.assign({}, DEFAULTS.pressure, (brush && brush.pressure) || {});
    out.size = Math.max(1, +out.size);
    out.opacity = clamp(+out.opacity, 0, 1);
    out.flow = clamp(+out.flow, 0.01, 1);
    out.hardness = clamp(+out.hardness, 0, 1);
    out.spacing = clamp(+out.spacing, 0.01, 2);
    out.aspect = clamp(+out.aspect, 0.02, 1);
    out.angle = +out.angle;
    out.follow = clamp(+out.follow, 0, 1);
    out.eraser = !!out.eraser;
    return out;
  }

  function parseColor(value) {
    var m = /^#?([0-9a-f]{6})$/i.exec((value || '#000000').trim());
    if (!m) return [0, 0, 0];
    var hex = m[1];
    return [
      parseInt(hex.slice(0, 2), 16),
      parseInt(hex.slice(2, 4), 16),
      parseInt(hex.slice(4, 6), 16)
    ];
  }

  /* La dureté n'entre volontairement pas dans la clé — voir la note détaillée
   * dans brush_engine.shape_seed, dont cette fonction est le double exact :
   * l'aperçu et le rendu d'export doivent produire la même forme. */
  function shapeSeed(brush) {
    var key = brush.shape + '|' + brush.aspect.toFixed(4);
    var h = 2166136261;
    for (var i = 0; i < key.length; i++) {
      h = Math.imul(h ^ key.charCodeAt(i), 16777619);
    }
    return h >>> 0;
  }

  /* --------------------------------------------- Textures utilisateur */

  var textures = {};

  function loadTexture(dataUrl) {
    if (textures[dataUrl]) return Promise.resolve(textures[dataUrl]);
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        var cv = document.createElement('canvas');
        cv.width = cv.height = BASE_RES;
        var c = cv.getContext('2d');
        c.drawImage(img, 0, 0, BASE_RES, BASE_RES);
        textures[dataUrl] = c.getImageData(0, 0, BASE_RES, BASE_RES);
        resolve(textures[dataUrl]);
      };
      img.onerror = function () { reject(new Error('Texture illisible')); };
      img.src = dataUrl;
    });
  }

  /* ------------------------------------------------ Masques de reference */

  function smoothstep(e0, e1, x) {
    if (e1 <= e0) return x <= e0 ? 1 : 0;
    var t = clamp((x - e0) / (e1 - e0), 0, 1);
    return t * t * (3 - 2 * t);
  }

  /* Adoucissement minimum des bords, en pixels de sortie — miroir de
   * MIN_EDGE_PX cote Python. Le masque est bati en 128 px puis reduit par le
   * navigateur : on calibre donc le plancher sur la taille nominale du pinceau,
   * pas sur 128. */
  var MIN_EDGE_PX = 1.25;

  function minSoft(res) {
    return Math.min(0.5, 2 * MIN_EDGE_PX / Math.max(res, 1));
  }

  function buildBaseMask(brush) {
    var res = BASE_RES;
    var mask = new Float32Array(res * res);
    var shape = brush.shape;
    var softRes = Math.max(1, brush.size);

    if (shape === 'texture' && brush.texture) {
      var data = textures[brush.texture];
      if (!data) return maskRound(mask, res, brush.hardness); // texture pas encore prete
      var px = data.data;
      var maxAlpha = 0;
      for (var t = 0; t < res * res; t++) maxAlpha = Math.max(maxAlpha, px[t * 4 + 3]);
      for (var k = 0; k < res * res; k++) {
        mask[k] = maxAlpha < 3
          ? 1 - (px[k * 4] + px[k * 4 + 1] + px[k * 4 + 2]) / 765
          : px[k * 4 + 3] / 255;
      }
      return mask;
    }

    if (shape === 'rect') return maskRect(mask, res, brush, softRes);
    if (shape === 'speckle') return maskBlobs(mask, res, brush, 16, 0.62, 0.12, 0.2, 0.7, 0.55, 0.45, brush.hardness * 0.6);
    if (shape === 'grain') return maskBlobs(mask, res, brush, 140, 0.85, 0.03, 0.05, 0.9, 0.45, 0.55, 0.35);
    return maskRound(mask, res, brush.hardness, softRes);
  }

  function axisValue(i, res) {
    return ((i + 0.5) / res) * 2 - 1;
  }

  function maskRound(mask, res, hardness, softRes) {
    var inner = clamp(hardness, 0, 1 - minSoft(softRes || res));
    for (var y = 0; y < res; y++) {
      var vy = axisValue(y, res);
      for (var x = 0; x < res; x++) {
        var vx = axisValue(x, res);
        mask[y * res + x] = 1 - smoothstep(inner, 1, Math.hypot(vx, vy));
      }
    }
    return mask;
  }

  function maskRect(mask, res, brush, softRes) {
    var halfH = brush.aspect;
    var soft = Math.max(minSoft(softRes || res), (1 - brush.hardness) * 0.5);
    for (var y = 0; y < res; y++) {
      var vy = Math.abs(axisValue(y, res));
      var my = 1 - smoothstep(Math.max(0, halfH - soft), halfH, vy);
      for (var x = 0; x < res; x++) {
        var vx = Math.abs(axisValue(x, res));
        mask[y * res + x] = (1 - smoothstep(1 - soft, 1, vx)) * my;
      }
    }
    return mask;
  }

  function maskBlobs(mask, res, brush, count, spread, minR, varR, softness, minA, varA, envelope) {
    var rng = new Rng(shapeSeed(brush));
    var inner = Math.max(0, 1 - softness);
    for (var i = 0; i < count; i++) {
      var angle = rng.next() * Math.PI * 2;
      var radius = Math.sqrt(rng.next()) * spread;
      var cx = Math.cos(angle) * radius;
      var cy = Math.sin(angle) * radius;
      var blobR = minR + rng.next() * varR;
      var alpha = minA + rng.next() * varA;

      var x0 = Math.max(0, Math.floor(((cx - blobR + 1) / 2) * res));
      var x1 = Math.min(res, Math.ceil(((cx + blobR + 1) / 2) * res) + 1);
      var y0 = Math.max(0, Math.floor(((cy - blobR + 1) / 2) * res));
      var y1 = Math.min(res, Math.ceil(((cy + blobR + 1) / 2) * res) + 1);

      for (var y = y0; y < y1; y++) {
        var vy = axisValue(y, res) - cy;
        for (var x = x0; x < x1; x++) {
          var vx = axisValue(x, res) - cx;
          var value = (1 - smoothstep(inner, 1, Math.hypot(vx, vy) / blobR)) * alpha;
          var idx = y * res + x;
          if (value > mask[idx]) mask[idx] = value;
        }
      }
    }
    for (var y2 = 0; y2 < res; y2++) {
      var ey = axisValue(y2, res);
      for (var x2 = 0; x2 < res; x2++) {
        var ex = axisValue(x2, res);
        mask[y2 * res + x2] *= 1 - smoothstep(clamp(envelope, 0, 0.98), 1, Math.hypot(ex, ey));
      }
    }
    return mask;
  }

  /* --------------------------------------------- Empreinte teintee (cache) */

  var stampCache = {};

  function brushKey(brush) {
    // La taille intervient : elle calibre le plancher d'adoucissement des bords.
    return [brush.shape, brush.hardness.toFixed(4), brush.aspect.toFixed(4),
      Math.round(brush.size), brush.texture || '', brush.color].join('|');
  }

  function getStamp(brush) {
    var key = brushKey(brush);
    var cached = stampCache[key];
    if (cached) return cached;

    var mask = buildBaseMask(brush);
    var canvas = document.createElement('canvas');
    canvas.width = canvas.height = BASE_RES;
    var ctx = canvas.getContext('2d');
    var image = ctx.createImageData(BASE_RES, BASE_RES);
    var rgb = parseColor(brush.color);
    for (var i = 0; i < BASE_RES * BASE_RES; i++) {
      image.data[i * 4] = rgb[0];
      image.data[i * 4 + 1] = rgb[1];
      image.data[i * 4 + 2] = rgb[2];
      image.data[i * 4 + 3] = Math.round(clamp(mask[i], 0, 1) * 255);
    }
    ctx.putImageData(image, 0, 0);
    stampCache[key] = canvas;
    return canvas;
  }

  function clearCache() {
    stampCache = {};
  }

  /* --------------------------------------------------- Trace en streaming */

  /** Repartit les empreintes le long du trace, au fil des points recus.
   *  L'algorithme est identique a `plan_stroke()` cote Python. */
  function StrokeStamper(brush, seed, ctx) {
    this.brush = normalize(brush);
    this.rng = new Rng(seed);
    this.ctx = ctx;
    // Le tampon est bati en 128 px et redimensionne a chaque pose : sans
    // filtrage de qualite, la reduction crenelle les bords.
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    this.stamp = getStamp(this.brush);
    this.step = Math.max(0.5, this.brush.spacing * this.brush.size);
    this.carry = 0;
    this.last = null;
  }

  StrokeStamper.prototype.begin = function (point) {
    this.last = point;
    this.carry = 0;
    this._emit(point.x, point.y, pressureOf(point), 0);
  };

  StrokeStamper.prototype.extend = function (point) {
    var p0 = this.last;
    if (!p0) { this.begin(point); return; }

    var dx = point.x - p0.x;
    var dy = point.y - p0.y;
    var seg = Math.hypot(dx, dy);
    if (seg < 1e-6) return;

    var direction = Math.atan2(dy, dx) / DEG;
    var pr0 = pressureOf(p0);
    var pr1 = pressureOf(point);
    var travelled = this.step - this.carry;

    while (travelled <= seg) {
      var u = travelled / seg;
      this._emit(p0.x + dx * u, p0.y + dy * u, pr0 + (pr1 - pr0) * u, direction);
      travelled += this.step;
    }
    this.carry = seg - (travelled - this.step);
    this.last = point;
  };

  StrokeStamper.prototype._emit = function (x, y, pressure, direction) {
    var brush = this.brush;
    var jitter = brush.jitter;
    var rng = this.rng;

    var size = brush.size * (1 - brush.pressure.size + brush.pressure.size * pressure);
    if (jitter.size) size *= Math.max(0.05, 1 + rng.sym() * jitter.size);

    var angle = brush.angle + direction * brush.follow;
    if (jitter.angle) angle += rng.sym() * jitter.angle;

    var alpha = brush.flow * (1 - brush.pressure.opacity + brush.pressure.opacity * pressure);
    if (jitter.opacity) alpha *= Math.max(0, 1 - rng.next() * jitter.opacity);

    var px = x;
    var py = y;
    if (jitter.position) {
      var radius = jitter.position * size;
      px += rng.sym() * radius;
      py += rng.sym() * radius;
    }

    size = Math.max(1, size);
    var ctx = this.ctx;
    ctx.save();
    ctx.globalAlpha = clamp(alpha, 0, 1);
    ctx.translate(px, py);
    if (angle) ctx.rotate(angle * DEG);
    ctx.drawImage(this.stamp, -size / 2, -size / 2, size, size);
    ctx.restore();
  };

  function pressureOf(point) {
    var value = point && point.p;
    return typeof value === 'number' ? clamp(value, 0, 1) : 1;
  }

  /* ------------------------------------------------------ Vignette d'apercu */

  /** Dessine un trait d'exemple — sert d'icone dans la bibliotheque. */
  function drawPreview(canvas, brush, options) {
    var opts = options || {};
    var normalized = normalize(brush);
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    var preview = Object.assign({}, normalized);
    // Une gomme se lit en clair : sur le gris de l'interface, un gris moyen
    // serait invisible.
    if (normalized.eraser) preview.color = '#e8e8e8';
    if (opts.maxSize) {
      preview.size = Math.min(normalized.size, opts.maxSize);
    }

    var scratch = document.createElement('canvas');
    scratch.width = canvas.width;
    scratch.height = canvas.height;
    var stamper = new StrokeStamper(preview, 1337, scratch.getContext('2d'));

    var midY = canvas.height / 2;
    var amplitude = Math.min(canvas.height / 3.2, 10);
    var margin = Math.min(canvas.width * 0.12, preview.size);
    var steps = 40;
    for (var i = 0; i <= steps; i++) {
      var u = i / steps;
      var point = {
        x: margin + (canvas.width - margin * 2) * u,
        y: midY + Math.sin(u * Math.PI * 1.6) * amplitude,
        p: 0.35 + 0.65 * Math.sin(u * Math.PI)
      };
      if (i === 0) stamper.begin(point); else stamper.extend(point);
    }

    ctx.globalAlpha = normalized.opacity;
    ctx.drawImage(scratch, 0, 0);
    ctx.globalAlpha = 1;
  }

  global.BrushEngine = {
    BASE_RES: BASE_RES,
    Rng: Rng,
    normalize: normalize,
    setDefaults: setDefaults,
    parseColor: parseColor,
    getStamp: getStamp,
    clearCache: clearCache,
    loadTexture: loadTexture,
    StrokeStamper: StrokeStamper,
    drawPreview: drawPreview
  };
})(window);
