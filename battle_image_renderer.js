(function () {
  'use strict';

  const WIDTH = 2400;
  const MARGIN = 44;
  const GAP = 24;
  const PANEL_WIDTH = (WIDTH - MARGIN * 2 - GAP) / 2;
  const FONT = '"Microsoft YaHei", "Segoe UI", sans-serif';
  const VEHICLE_FONT = `WTSymbols, ${FONT}`;
  const FLAG_CODES = { '美国': 'us', '德国': 'de', '苏联': 'su', '英国': 'gb', '日本': 'jp',
    '意大利': 'it', '法国': 'fr', '瑞典': 'se', '中国': 'cn', '以色列': 'il' };
  const COLORS = {
    background: '#101821', panel: '#1b2836', card: '#162330', line: '#34485c',
    text: '#eef4fa', muted: '#b2c4d3', blue: '#91c7f4', red: '#f2a6a5',
    gold: '#ffd17d', highlight: '#344337', highlightLine: '#e8c365',
  };

  function value(item) {
    return item === null || item === undefined || item === '' ? 'N/A' : String(item);
  }

  function rating(item) {
    const number = Number(item);
    return item === null || item === undefined || !Number.isFinite(number) ? 'N/A' : number.toFixed(1);
  }

  function country(player) {
    const countries = [...new Set((player.configured || []).map(item => item.country).filter(item => item && item !== 'N/A'))];
    return countries.length === 1 ? countries[0] : 'N/A';
  }

  function flagCode(player) { return FLAG_CODES[country(player)] || null; }

  function loadFlag(code) {
    return new Promise(resolve => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => resolve(null);
      image.src = `/flags/${code}.svg`;
    });
  }

  async function loadFlags(players) {
    const codes = [...new Set(players.map(flagCode).filter(Boolean))];
    return new Map(await Promise.all(codes.map(async code => [code, await loadFlag(code)])));
  }

  function drawFlag(ctx, image, x, y, width, height) {
    ctx.drawImage(image, x, y, width, height);
    ctx.strokeStyle = COLORS.line;
    ctx.lineWidth = 1;
    ctx.strokeRect(x, y, width, height);
  }

  function aiText(player) {
    const fields = [player.aiKills, player.aiGroundKills, player.aiNavalKills];
    if (fields.some(item => item === null || item === undefined || item === '' || !Number.isFinite(Number(item)))) return 'AI N/A';
    const counts = fields.map(Number);
    const parts = ['空中', '地面', '海上'].map((label, index) => counts[index] ? `${label} ${counts[index]}` : '').filter(Boolean);
    return `AI ${counts.reduce((sum, count) => sum + count, 0)}${parts.length ? `（${parts.join(' · ')}）` : ''}`;
  }

  function roundRect(ctx, x, y, width, height, radius, color) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + width, y, x + width, y + height, radius);
    ctx.arcTo(x + width, y + height, x, y + height, radius);
    ctx.arcTo(x, y + height, x, y, radius);
    ctx.arcTo(x, y, x + width, y, radius);
    ctx.closePath();
    ctx.fill();
  }

  function wrap(ctx, text, maxWidth) {
    const lines = [];
    for (const paragraph of String(text).split('\n')) {
      let line = '';
      for (const char of paragraph) {
        if (line && ctx.measureText(line + char).width > maxWidth) {
          lines.push(line);
          line = char;
        } else {
          line += char;
        }
      }
      lines.push(line || ' ');
    }
    return lines;
  }

  function drawLines(ctx, lines, x, y, lineHeight, color) {
    ctx.fillStyle = color;
    for (const line of lines) {
      ctx.fillText(line, x, y);
      y += lineHeight;
    }
    return y;
  }

  function playerLayout(ctx, player) {
    const contentWidth = PANEL_WIDTH - 64;
    ctx.font = `700 30px ${FONT}`;
    const name = wrap(ctx, value(player.name), contentWidth - 210);
    ctx.font = `18px ${VEHICLE_FONT}`;
    const vehicles = wrap(ctx, `载具  ${(player.configured || []).map(item => value(item.name)).join(' · ') || 'N/A'}`, contentWidth);
    ctx.font = `16px ${FONT}`;
    const other = wrap(ctx, `助攻 ${value(player.assists)}   ·   UID ${value(player.uid)}   ·   ${aiText(player)}   ·   占点 ${value(player.captureZone)}   ·   击杀队友 ${value(player.teamKills)}`, contentWidth);
    const metricsOffset = 24 + name.length * 38;
    const height = metricsOffset + 62 + 12 + 18 + vehicles.length * 24 + 5 + other.length * 22 + 5;
    return { player, name, vehicles, other, height, metricsOffset, flagCode: flagCode(player) };
  }

  function teamLayout(ctx, team, players) {
    const sorted = players.map((player, index) => ({ player, index })).sort((a, b) => {
      const aScore = Number(a.player.score), bScore = Number(b.player.score);
      const aMissing = a.player.score === null || a.player.score === undefined || a.player.score === '' || !Number.isFinite(aScore);
      const bMissing = b.player.score === null || b.player.score === undefined || b.player.score === '' || !Number.isFinite(bScore);
      return aMissing !== bMissing ? (aMissing ? 1 : -1) : aMissing ? a.index - b.index : bScore - aScore || a.index - b.index;
    }).map(item => item.player);
    const rows = sorted.map(player => playerLayout(ctx, player));
    const ratings = players.map(player => player.max_br).filter(br => br !== null && br !== undefined && br !== '' && Number.isFinite(Number(br))).map(Number);
    return { team, rows, maxBr: ratings.length ? Math.max(...ratings) : null,
      height: 80 + (rows.length ? rows.reduce((sum, row) => sum + row.height + 8, 0) : 66) + 12 };
  }

  function drawTeam(ctx, layout, x, y, highlightUid, flagImages) {
    const blue = String(layout.team) === '1';
    const red = String(layout.team) === '2';
    const accent = blue ? COLORS.blue : red ? COLORS.red : COLORS.muted;
    const title = blue ? '蓝队' : red ? '红队' : `队伍 ${value(layout.team)}`;
    roundRect(ctx, x, y, PANEL_WIDTH, layout.height, 15, COLORS.panel);
    roundRect(ctx, x + 12, y + 12, PANEL_WIDTH - 24, 56, 8, blue ? '#203d54' : red ? '#4a3039' : '#344456');
    ctx.font = `700 26px ${FONT}`;
    ctx.fillStyle = accent;
    const heading = `${title} · ${layout.rows.length} 人 · 最高权重 ${rating(layout.maxBr)}`;
    ctx.fillText(heading, x + 30, y + 49);
    let flagX = x + 46 + ctx.measureText(heading).width;
    for (const code of new Set(layout.rows.map(row => row.flagCode).filter(Boolean))) {
      const image = flagImages.get(code);
      if (image) {
        drawFlag(ctx, image, flagX, y + 29, 31, 22);
        flagX += 36;
      }
    }
    let rowY = y + 80;
    if (!layout.rows.length) {
      ctx.font = `20px ${FONT}`;
      ctx.fillStyle = COLORS.muted;
      ctx.fillText('暂无玩家结算数据', x + 30, rowY + 35);
    }
    for (const row of layout.rows) {
      const selected = highlightUid !== null && String(row.player.uid) === highlightUid;
      roundRect(ctx, x + 14, rowY, PANEL_WIDTH - 28, row.height, 9, selected ? COLORS.highlight : COLORS.card);
      if (selected) roundRect(ctx, x + 14, rowY, 5, row.height, 2, COLORS.highlightLine);
      const left = x + 32;
      ctx.font = `700 30px ${FONT}`;
      drawLines(ctx, row.name, left, rowY + 39, 38, selected ? COLORS.gold : COLORS.text);
      const separatorX = left + ctx.measureText(row.name[row.name.length - 1]).width + 9;
      const nameBaseline = rowY + 39 + (row.name.length - 1) * 38;
      ctx.fillText('·', separatorX, nameBaseline);
      const flag = flagImages.get(row.flagCode);
      if (flag) drawFlag(ctx, flag, separatorX + 23, nameBaseline - 27, 46, 31);
      else {
        ctx.font = `20px ${FONT}`;
        ctx.fillStyle = COLORS.muted;
        ctx.fillText('N/A', separatorX + 23, nameBaseline);
      }
      ctx.font = `700 20px ${FONT}`;
      ctx.fillStyle = selected ? COLORS.gold : accent;
      ctx.textAlign = 'right';
      ctx.fillText(`BR ${rating(row.player.max_br)}`, x + PANEL_WIDTH - 32, rowY + 38);
      ctx.textAlign = 'left';
      const metrics = [
        ['得分', row.player.score], ['对空击杀', row.player.kills],
        ['对地击杀', row.player.groundKills], ['死亡', row.player.deaths],
      ];
      const metricsY = rowY + row.metricsOffset;
      const metricGap = 10, metricWidth = (PANEL_WIDTH - 64 - metricGap * 3) / 4;
      metrics.forEach(([label, amount], index) => {
        const metricX = left + index * (metricWidth + metricGap);
        roundRect(ctx, metricX, metricsY, metricWidth, 62, 6, selected ? '#405143' : '#213245');
        ctx.font = `16px ${FONT}`;
        ctx.fillStyle = COLORS.muted;
        ctx.fillText(label, metricX + 12, metricsY + 21);
        ctx.font = `700 28px ${FONT}`;
        ctx.fillStyle = index === 0 ? COLORS.gold : COLORS.text;
        ctx.fillText(value(amount), metricX + 12, metricsY + 52);
      });
      ctx.font = `18px ${VEHICLE_FONT}`;
      const afterVehicles = drawLines(ctx, row.vehicles, left, metricsY + 62 + 12 + 18, 24, COLORS.text);
      ctx.font = `16px ${FONT}`;
      drawLines(ctx, row.other, left, afterVehicles + 5, 22, COLORS.muted);
      rowY += row.height + 8;
    }
  }

  function metaLayout(ctx, label, text) {
    ctx.font = `22px ${FONT}`;
    const lines = wrap(ctx, text, (WIDTH - 2 * MARGIN - 2 * GAP) / 3 - 38);
    return { label, lines };
  }

  async function render(data, highlightUid = null) {
    await document.fonts.load('18px WTSymbols', '␗▄◔');
    const flagImages = await loadFlags(data.players || []);
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('浏览器无法创建图片画布。');
    const groups = new Map([[1, []], [2, []]]);
    for (const player of data.players || []) {
      const team = Number(player.team);
      if (!groups.has(team)) groups.set(team, []);
      groups.get(team).push(player);
    }
    const teams = [...groups].map(([team, players]) => teamLayout(ctx, team, players));
    const groupLabels = { aircraft: '空战', aviation: '空战', air: '空战', tank: '陆战', ground: '陆战', ship: '海战' };
    const modeLabels = { arcade: '街机', realistic: '历史', simulation: '全真' };
    const metadata = [
      metaLayout(ctx, '开始时间 (UTC)', value(data.start_utc)),
      metaLayout(ctx, '地图', value(data.map || data.level)),
      metaLayout(ctx, 'Session ID', value(data.sid)),
      metaLayout(ctx, '对局模式', `${value(groupLabels[data.battle_group] || data.battle_group)} · ${value(modeLabels[data.game_mode] || data.game_mode)}`),
      metaLayout(ctx, '对局权重', rating(data.match_br)),
      metaLayout(ctx, '数据来源', `${value(data.source)} · ${value(data.parts)} 段`),
    ];
    const metaHeight = [0, 3].map(start => Math.max(...metadata.slice(start, start + 3).map(item => 40 + item.lines.length * 29)) + 12);
    const teamsTop = 104 + metaHeight[0] + metaHeight[1] + 62;
    const teamRows = [];
    let totalTeamHeight = 0;
    for (let index = 0; index < teams.length; index += 2) {
      const height = Math.max(...teams.slice(index, index + 2).map(item => item.height));
      teamRows.push({ index, y: teamsTop + totalTeamHeight });
      totalTeamHeight += height + GAP;
    }
    canvas.width = WIDTH;
    canvas.height = teamsTop + totalTeamHeight + 18;
    ctx.fillStyle = COLORS.background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.font = `700 34px ${FONT}`;
    ctx.fillStyle = COLORS.text;
    ctx.fillText('WTStat · 对局详情', MARGIN, 65);
    ctx.font = `18px ${FONT}`;
    ctx.fillStyle = COLORS.muted;
    ctx.textAlign = 'right';
    ctx.fillText('最终战绩 · 队内按得分从高到低', WIDTH - MARGIN, 60);
    ctx.textAlign = 'left';
    const metaWidth = (WIDTH - 2 * MARGIN - 2 * GAP) / 3;
    let metaY = 104;
    for (let row = 0; row < 2; row++) {
      const height = metaHeight[row] - 12;
      for (let column = 0; column < 3; column++) {
        const item = metadata[row * 3 + column];
        const x = MARGIN + column * (metaWidth + GAP);
        roundRect(ctx, x, metaY, metaWidth, height, 9, COLORS.card);
        ctx.font = `16px ${FONT}`;
        ctx.fillStyle = COLORS.muted;
        ctx.fillText(item.label, x + 18, metaY + 26);
        ctx.font = `22px ${FONT}`;
        drawLines(ctx, item.lines, x + 18, metaY + 58, 29, COLORS.text);
      }
      metaY += metaHeight[row];
    }
    ctx.font = `17px ${FONT}`;
    ctx.fillStyle = COLORS.muted;
    ctx.fillText(`官网对局页  https://warthunder.com/en/tournament/replay/${BigInt(`0x${data.sid}`)}`, MARGIN, metaY + 25);
    for (const row of teamRows) {
      drawTeam(ctx, teams[row.index], MARGIN, row.y, highlightUid, flagImages);
      if (teams[row.index + 1]) drawTeam(ctx, teams[row.index + 1], MARGIN + PANEL_WIDTH + GAP, row.y, highlightUid, flagImages);
    }
    return canvas;
  }

  window.WTStatBattleImage = { render };
})();
