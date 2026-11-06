import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import "./tug.css";

/**
 * BULL vs BEAR on the scalp ticket's stop-to-target bar: a tug of war for the price. Chart dressing only -
 * it reads the same `at` / `entryAt` as the bar and changes nothing about the trade.
 *
 * The trade's own side holds the target end (a long's bull on the right, a short's bear), the other side
 * the stop end. Each champion's mood is how far price has gone toward its end, measured from the entry:
 * at the entry both pull evenly; on the way to the stop the trade's side strains, then is spent (sweat,
 * breath, dragged forward); near the target the other side is. When the trade closes the winner gets
 * a short cinematic (TugParty), and a target adds the gold (TugConfetti).
 *
 * Their legs walk while they pull: backing away step by step, and stumbling forward when they are dragged.
 *
 * Drawn in SVG and animated in CSS (tug.css): transforms and opacity only, so it costs next to nothing
 * beside the live chart, and it holds still for prefers-reduced-motion.
 */

export type Kind = "bull" | "bear";
type Mood = "win" | "pull" | "strain" | "spent";
type Pose = "pull" | "cheer" | "slump";

// How far price has gone toward this end, from the entry: 0.5 at the entry, 1 at this end, 0 at the other.
function toward(at: number, entryAt: number): number {
  const x = Math.max(0, Math.min(1, at)), e = Math.max(0.01, Math.min(0.99, entryAt));
  return x >= e ? 0.5 + (0.5 * (x - e)) / (1 - e) : (0.5 * x) / e;
}
const moodOf = (a: number): Mood => (a >= 0.75 ? "win" : a >= 0.4 ? "pull" : a >= 0.2 ? "strain" : "spent");

export const winnerOf = (dir: "long" | "short", pnl: number): Kind => ((pnl >= 0) === (dir === "long") ? "bull" : "bear");

// Gradient ids must be unique on the page: React's ids, without the colons.
const useUid = () => "tug" + useId().replace(/[^a-zA-Z0-9]/g, "");

// The drawings: the bull 120 x 70 on all fours, the bear 80 x 70 standing; the rope runs at y 28 in both,
// 0.4 of the height down. On the bar it must land on the bar's middle, 24 px down the 58 px stage (tug.css),
// so a champion h px tall stands 58 - 24 - 0.6 h px off the stage's floor.
const VB: Record<Kind, number> = { bull: 120, bear: 80 };
const HEIGHT: Record<Kind, number> = { bull: 54, bear: 56 };
const widthOf = (k: Kind, h = HEIGHT[k]) => (h * VB[k]) / 70;
const footing = (h: number) => 58 - 24 - 0.6 * h;

// The rope's twist is a row of slanted strands that slides along it (tug.css): in toward a champion
// pulling it, out toward the middle when it is being dragged, and a little with every heave.
function Rope({ id, x, w }: { id: string; x: number; w: number }) {
  let strands = "";
  for (let s = x - 6.4; s < x + w + 6.4; s += 3.2) strands += `M${s.toFixed(1)} 25.6h1.3l-2.6 4.8h-1.3Z`;
  return (
    <g>
      <clipPath id={`${id}c`}><rect x={x} y="25.6" width={w} height="4.8" rx="2.4" /></clipPath>
      <rect x={x} y="25.6" width={w} height="4.8" rx="2.4" fill={`url(#${id}r)`} />
      <g clipPath={`url(#${id}c)`}>
        <path className="tug-twist" d={strands} fill="#000" fillOpacity=".4" />
      </g>
    </g>
  );
}

function Puffs({ id, x, y }: { id: string; x: number; y: number }) {
  return (
    <g className="tug-breath">
      {[0, 1, 2].map((i) => (
        <circle key={i} cx={x} cy={y} r="3.2" fill={`url(#${id}p)`} style={{ transformOrigin: `${x}px ${y}px`, animationDelay: `${i * 0.37}s` }} />
      ))}
    </g>
  );
}

function Sweat({ x, y }: { x: number; y: number }) {
  return (
    <g className="tug-sweat">
      {[0, 1].map((i) => (
        <path
          key={i}
          d={`M${x + i * 3.5} ${y} c-1.2 1.9 -1.8 3 -1.8 3.8 a1.8 1.8 0 0 0 3.6 0 c0 -.8 -.6 -1.9 -1.8 -3.8 Z`}
          fill="#bfe3ff"
          style={{ animationDelay: `${i * 0.55}s` }}
        />
      ))}
    </g>
  );
}

function Dust({ x }: { x: number }) {
  return (
    <g className="tug-dust">
      {[0, 1, 2, 3].map((i) => (
        <circle key={i} cx={x - i * 2.5} cy="67.5" r={1.4 + (i % 2) * 0.6} fill="#a8916f" style={{ transformOrigin: `${x - i * 2.5}px 67.5px`, animationDelay: `${i * 0.21}s` }} />
      ))}
    </g>
  );
}

function Defs({ id, fur }: { id: string; fur: [string, string, string] }) {
  return (
    <defs>
      <linearGradient id={`${id}f`} gradientUnits="userSpaceOnUse" x1="0" y1="10" x2="0" y2="69">
        <stop offset="0" stopColor={fur[0]} /><stop offset=".45" stopColor={fur[1]} /><stop offset="1" stopColor={fur[2]} />
      </linearGradient>
      <linearGradient id={`${id}n`} x1="0" y1="1" x2="1" y2="0">
        <stop offset="0" stopColor="#6b5d4a" /><stop offset=".55" stopColor="#e6dbc3" /><stop offset="1" stopColor="#fffaf0" />
      </linearGradient>
      <linearGradient id={`${id}r`} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#ecd3a2" /><stop offset=".5" stopColor="#a8824c" /><stop offset="1" stopColor="#4e3a20" />
      </linearGradient>
      <radialGradient id={`${id}p`}>
        <stop offset="0" stopColor="#e8eef5" stopOpacity=".75" /><stop offset="1" stopColor="#e8eef5" stopOpacity="0" />
      </radialGradient>
      <radialGradient id={`${id}s`}>
        <stop offset="0" stopColor="#000" stopOpacity=".75" /><stop offset="1" stopColor="#000" stopOpacity="0" />
      </radialGradient>
    </defs>
  );
}

// The bull, facing right: hump, lowered head, the rope tied round its horns.
function Bull({ pose }: { pose: Pose }) {
  const id = useUid();
  const u = (k: string) => `url(#${id}${k})`;
  return (
    <svg viewBox="0 0 120 70" aria-hidden>
      <Defs id={id} fur={["#5a4030", "#2b1d15", "#0c0806"]} />
      <ellipse cx="52" cy="68.6" rx="48" ry="3.4" fill={u("s")} />
      <g className="tug-pose" style={{ transformOrigin: "24px 68px" }}>
        {pose === "pull" && <Rope id={id} x={93} w={31} />}
        {/* The legs walk (tug.css): each one swings about its hip, a quarter step after the one before. */}
        <g className="tug-leg tug-leg-ff" style={{ transformOrigin: "66px 41px" }}>
          <path d="M62 40 L70 41 C72 50 75 57 78 62 L80 67 L74 68 L72 63 C68 56 64 49 62 45 Z" fill="#0b0705" />
        </g>
        <g className="tug-leg tug-leg-fh" style={{ transformOrigin: "31px 40px" }}>
          <path d="M30 38 C37 40 40 45 38 51 L33 58 L35 66.5 L29 67.8 L27.5 60 C24.5 55 23.5 49 24 43 Z" fill="#0b0705" />
        </g>
        <g className="tug-lean" style={{ transformOrigin: "94px 28px" }}>
          <g className="tug-heave" style={{ transformOrigin: "94px 28px" }}>
            <g className="tug-tail" style={{ transformOrigin: "17px 32px" }}>
              <path d="M17 32 C10 35 7 43 8.5 53" fill="none" stroke="#1a110c" strokeWidth="1.6" strokeLinecap="round" />
              <ellipse cx="8.6" cy="55" rx="2" ry="3.4" fill="#120b08" />
            </g>
            <path
              d="M22 30 C30 24 44 26 54 22 C60 19 66 13.5 72 15.5 C78 17.5 83 21.5 87 27 C89 33 88 42 85 47 C82 51 78 53 74 52
                 C71 51 69.5 49.5 68 48.5 C60 49.5 46 49.5 35 47.5 C27 46.5 19 44.5 15.5 39.5 C13.5 35.5 15.5 31 22 30 Z"
              fill={u("f")}
            />
            <path d="M22 30 C30 24 44 26 54 22 C60 19 66 13.5 72 15.5 C78 17.5 82 22 85 26 C78 29.5 64 32 50 33 C36 34 25 33 22 30 Z" fill="#b08560" fillOpacity=".22" />
            <g className="tug-head" style={{ transformOrigin: "94px 28px" }}>
              <path d="M85 27.5 C85.5 22 88 18 92 16 C94 15 96.5 14.8 98.5 15.5 C95.5 16.5 93 18.5 91.3 21.5 C90 24 89.5 26.5 89.5 28.5 Z" fill={u("n")} opacity=".6" />
              <path
                d="M80 22 C84 23 88 25.5 91 28.5 C95 31 98 34.5 100.5 38.5 C103 42 105.5 46 104.5 49.5 C103.5 52.5 98.5 53 95.5 51.5
                   C92 50 88.5 48.5 85 46.5 C80.5 43 78 30 80 22 Z"
                fill={u("f")}
              />
              <ellipse cx="100.5" cy="47.8" rx="3.6" ry="2.9" fill="#5a4030" fillOpacity=".55" />
              <path d="M87.5 29.5 C89 24 92.5 20.5 97 19.5 C100 18.8 103 19.5 105 21 C101.5 21 98.5 22 96 24 C94 25.7 92.8 28 92.3 30.5 Z" fill={u("n")} />
              <path d="M85 31 C81 29.6 78 30.6 76.6 32.8 C79.6 33.8 83.4 33.4 86 32.2 Z" fill="#2e1f16" />
              <path d="M91.5 34.5 C93.5 33.4 96 33.6 97.6 35" fill="none" stroke="#0a0604" strokeWidth=".9" strokeLinecap="round" />
              <ellipse className="tug-eye" cx="95" cy="36.6" rx="1.25" ry="1.25" style={{ transformOrigin: "95px 36.6px" }} />
              <ellipse cx="102.6" cy="47.4" rx="1.1" ry=".8" fill="#000" />
              <circle cx="103.4" cy="50.6" r="1.7" fill="none" stroke="#e8b84a" strokeWidth=".7" />
              {pose === "pull" && <path d="M90.4 25.6 C92.6 31.8 96.8 31.8 98.4 27.2" fill="none" stroke={u("r")} strokeWidth="2.4" strokeLinecap="round" />}
              <Puffs id={id} x={105} y={49} />
              <Sweat x={88} y={22} />
            </g>
          </g>
        </g>
        <g className="tug-leg tug-leg-nf" style={{ transformOrigin: "71px 43px" }}>
          <path d="M67 42 L76 43 C78.5 50 81.5 56 85 61.5 L88.5 67 L82 68 L79 63 C75 57 70 51 67 47 Z" fill="#150e0a" />
          <path d="M82 68 L88.5 67 L89.6 68.7 L81.8 69 Z" fill="#000" />
        </g>
        <g className="tug-leg tug-leg-nh" style={{ transformOrigin: "22px 39px" }}>
          <path d="M18 36 C27 38 31.5 43.5 30 50 L24.5 57.5 L26 66.5 L19.5 68 L18.5 60 C15 55 13.8 49 14.5 43 Z" fill="#150e0a" />
          <path d="M19.5 68 L26 66.5 L27 68.5 L19.4 69 Z" fill="#000" />
        </g>
        <Dust x={88} />
      </g>
    </svg>
  );
}

// The bear: a grizzly standing on its hind legs and leaning back, both paws on the rope - shoulder hump,
// long snout, small ears, a snarl, claws. The far paw is behind the rope, the near one wraps over it.
function Bear({ pose }: { pose: Pose }) {
  const id = useUid();
  const u = (k: string) => `url(#${id}${k})`;
  const claws = (x: number, y: number) =>
    `M${x} ${y} q2 .9 1.6 3.2 M${x + 0.8} ${y + 2.4} q1.6 1 1 3 M${x - 0.2} ${y + 5} q1.2 .8 .4 2.2`;
  return (
    <svg viewBox="0 0 80 70" aria-hidden>
      <Defs id={id} fur={["#86603f", "#3e2a1b", "#0f0905"]} />
      <ellipse cx="30" cy="68.6" rx="27" ry="3.2" fill={u("s")} />
      <g className="tug-pose" style={{ transformOrigin: "30px 68px" }}>
        {/* the far hind leg, bent under it */}
        <g className="tug-leg tug-leg-fh" style={{ transformOrigin: "15px 47px" }}>
          <path d="M12 46 C18 47 22 51 21 56 C20.5 59 18.5 61.5 16.5 63.5 L18 66 C20.5 66.4 22.5 67.6 21.6 68.4 L8 68.4 C7.4 66.8 8.4 65.4 10 64.6 C11 60 10.5 54 12 46 Z" fill="#0d0805" />
        </g>
        <g className="tug-lean" style={{ transformOrigin: "58.6px 28px" }}>
          <g className="tug-heave" style={{ transformOrigin: "58.6px 28px" }}>
            <g className="tug-arm tug-arm-f" style={{ transformOrigin: "29px 19px" }}>
              <path d="M25.6 16.4 C31 15.2 36.4 17.4 40 20.4 C43.4 22.2 47 23.6 50.2 24.2 C52 24.4 52.4 30.4 50.4 30.8 C46.4 31 42 30.2 38.4 28.6 C32.8 26.6 28.2 23.4 25.6 20.6 Z" fill="#1a110a" />
              <path d="M48.4 23.8 C51.8 23 54.8 24.4 55.3 27.4 C55.7 30.2 53.9 31.8 51 31.8 C48.8 31.8 47.4 30.2 47.4 28 Z" fill="#120b07" />
              <path d={claws(53.6, 24.8)} fill="none" stroke="#d9ceb8" strokeOpacity=".7" strokeWidth=".45" strokeLinecap="round" />
            </g>
          </g>
        </g>
        {pose === "pull" && <Rope id={id} x={36} w={46} />}
        <g className="tug-lean" style={{ transformOrigin: "58.6px 28px" }}>
          <g className="tug-heave" style={{ transformOrigin: "58.6px 28px" }}>
            <path
              d="M14 51 C10 47 8.5 42 8.6 36 L7.2 34.5 L8.8 33.6 L7.6 31.5 L9.4 30.8 C9.6 24 11.5 18 15 14.5 C18 11.5 22 9.6 26.5 9.2
                 C30 9 33 11 34.5 13.5 C37 17 40.5 20.5 42.5 25 C44 29 44.5 34 43.5 39 L45 40.5 L43 41.2 L44.2 43.4 L42 43.6
                 C41 47 38 50.5 33.5 52.5 C27 55 19 54.5 14 51 Z"
              fill={u("f")}
            />
            <path d="M15 14.5 C18 11.5 22 9.6 26.5 9.2 C30 9 33 11 34.5 13.5 C30 14 24 15 18 19 C15.5 20.5 13.5 18 15 14.5 Z" fill="#dcb98e" fillOpacity=".26" />
            <path d="M13 22 l2.6 -2 M12 27 l2.8 -1.4 M17 17 l2.8 -1.6 M21.5 13.6 l2.8 -1" stroke="#ead2aa" strokeOpacity=".35" strokeWidth=".55" strokeLinecap="round" />
            <g className="tug-head" style={{ transformOrigin: "33px 13px" }}>
              <path d="M27.8 6.2 C26.6 3.2 28.6 1.2 30.8 1.6 C32.4 2 32.8 3.6 32 5.2 Z" fill="#20150d" />
              <path
                d="M26.5 10 C27 5.5 30.5 2.6 35 2.4 C39 2.2 42.4 3.6 44.2 6.4 C46.8 7.2 49.8 8 52 8.8 C53.8 9.5 54.6 10.8 54.2 12.2
                   C53.8 13.2 52.8 13.5 51.8 13.4 L45 13.8 C43 14.8 41 15.8 39.6 16.6 C36.5 17 33.4 16.2 30.8 14.8 C28.4 13.4 26.4 12.2 26.5 10 Z"
                fill={u("f")}
              />
              <path d="M30 4 C34 2.4 39.5 2.6 44.2 6.4 C39 6 34.5 5.8 30 4 Z" fill="#dcb98e" fillOpacity=".24" />
              <path d="M44.2 6.4 C46.8 7.2 49.8 8 52 8.8 C53.8 9.5 54.6 10.8 54.2 12.2 C53.8 13.2 52.8 13.5 51.8 13.4 L45 13.8 C44 11.5 43.6 8.8 44.2 6.4 Z" fill="#a8825c" fillOpacity=".5" />
              <path d="M31.6 4.6 C30.8 1.2 33.4 -.6 35.6 .2 C37.2 .9 37.4 2.8 36.4 4.4 Z" fill="#2c1c12" />
              <path d="M32.8 3.6 C32.6 1.8 33.8 1 35 1.4" fill="none" stroke="#0e0905" strokeWidth=".9" strokeLinecap="round" />
              <path d="M43.4 13.9 L52 13.4 L51.4 17.6 L43.4 17.2 Z" fill="#2a0707" />
              <path d="M49.4 13.5 L50 15.3 L50.6 13.46 Z M46.4 13.65 L46.9 14.9 L47.4 13.62 Z" fill="#f1e8d6" />
              <g className="tug-jaw" style={{ transformOrigin: "43.5px 15px" }}>
                <path d="M43.6 15.2 L50.8 15.9 C51.6 16.3 51.4 17.3 50.4 17.6 C47.8 18.6 44.4 18.9 41 18.4 C39 18.1 38.8 16 40.8 15.3 Z" fill={u("f")} />
                <path d="M48.8 15.8 L49.2 14.6 L49.7 15.85 Z" fill="#f1e8d6" />
              </g>
              <path d="M38.2 6 C39.6 5.2 41.6 5.2 43 6.2" fill="none" stroke="#0a0604" strokeWidth="1" strokeLinecap="round" />
              <ellipse className="tug-eye" cx="40.6" cy="7.3" rx=".95" ry=".95" style={{ transformOrigin: "40.6px 7.3px" }} />
              <ellipse cx="53" cy="10.4" rx="1.75" ry="1.35" fill="#000" />
              <ellipse cx="52.5" cy="9.9" rx=".5" ry=".3" fill="#fff" fillOpacity=".35" />
              <Puffs id={id} x={53} y={15} />
              <Sweat x={32} y={2} />
            </g>
            <g className="tug-arm tug-arm-n" style={{ transformOrigin: "30px 21px" }}>
              <path
                d="M26.5 18 C32 16.8 37.5 19 41 22 C45.5 23.8 50.5 24.6 55.4 24.8 C57.4 25 58 31 55.6 31.4 C50 31.8 44.5 31.6 39.4 30.4
                   L38.6 32.2 L37.2 30.6 L35.4 31.8 L34.6 29.8 C30.6 28.8 27.6 26.4 26 23.6 Z"
                fill={u("f")}
              />
              <path d="M54.6 24 C58.6 22.8 62.4 24 63.3 27.2 C64 30.2 62 32.3 58.6 32.3 C55.8 32.3 54.2 30.6 54.2 28.2 Z" fill="#24160d" />
              <path d={claws(62.4, 24.6)} fill="none" stroke="#efe6d2" strokeWidth=".6" strokeLinecap="round" />
            </g>
          </g>
        </g>
        {/* the near hind leg, braced forward */}
        <g className="tug-leg tug-leg-nh" style={{ transformOrigin: "27px 45px" }}>
          <path
            d="M22 44 C29 43 35 46.5 38.4 51 C40.6 54.5 42.4 58.5 43.8 62.4 L45.2 65.4 C49 65.6 53 66.6 53.6 68.4 L39 68.4 C38.6 66.8 38.8 65.4 39.6 64.4
               C37.8 61.2 35.4 58.4 32 56.4 C26 54.6 21.4 50.8 22 44 Z"
            fill={u("f")}
          />
          <path d="M53.2 67 q1.4 .2 2 1.4 M51.6 66.6 q1.4 .3 1.8 1.6 M50 66.4 q1.3 .3 1.6 1.6" fill="none" stroke="#efe6d2" strokeWidth=".5" strokeLinecap="round" />
        </g>
        <Dust x={52} />
      </g>
    </svg>
  );
}

const Animal = ({ kind, pose }: { kind: Kind; pose: Pose }) => (kind === "bull" ? <Bull pose={pose} /> : <Bear pose={pose} />);

// `rope`: which way the rope is moving at this end just now - "in" when this champion is gaining
// ground (it yanks and strides back: the bear hand over hand, the bull jerks back), "out" when it is
// dragged (it stumbles forward).
function Champ({ kind, side, mood, rope }: { kind: Kind; side: "left" | "right"; mood: Mood; rope?: "in" | "out" }) {
  return (
    <div
      className={`tug-champ tug-${kind} ${side}`}
      data-mood={mood}
      data-rope={rope}
      style={{ width: widthOf(kind), height: HEIGHT[kind], bottom: footing(HEIGHT[kind]) }}
    >
      <span className="tug-spot" />
      <Animal kind={kind} pose="pull" />
    </div>
  );
}

// The ribbon tied at the middle of the rope: it rides with the price marker.
function Knot({ x }: { x: number }) {
  return (
    <span className="tug-knot" style={{ left: `${x * 100}%` }}>
      <svg viewBox="0 0 10 14" aria-hidden>
        <path d="M5 0 L1 12 L3.4 10.6 L4.2 13.6 L5 1.5 Z" fill="#dc2626" />
        <path d="M5 0 L9 12 L6.6 10.6 L5.8 13.6 L5 1.5 Z" fill="#b91c1c" />
        <circle cx="5" cy="1.2" r="1.5" fill="#fbbf24" />
      </svg>
    </span>
  );
}

/** The stage around the bar: `children` is the bar itself (16 px tall), laid along the rope. */
export function TugOfWar({ dir, at, entryAt, children }: { dir: "long" | "short"; at: number; entryAt: number; children: ReactNode }) {
  const right: Kind = dir === "long" ? "bull" : "bear";   // the target end is the trade's own side
  const left: Kind = right === "bull" ? "bear" : "bull";
  const a = toward(at, entryAt);                          // the right (target) end's pull
  const edge = a <= 0.12 ? "sl" : a >= 0.9 ? "tp" : undefined;
  // A move of price is a pull on the rope: for under a second after each one, the rope slides that way
  // and the side it moved toward yanks while the other is dragged.
  const [pull, setPull] = useState<"l" | "r" | null>(null);
  const last = useRef(at);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    const d = at - last.current;
    last.current = at;
    if (Math.abs(d) < 0.002) return;
    setPull(d > 0 ? "r" : "l");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setPull(null), 900);
  }, [at]);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const ropeAt = (s: "l" | "r") => (pull == null ? undefined : pull === s ? "in" : "out");
  return (
    <div className="tug" data-edge={edge} data-pull={pull ?? undefined}>
      <div className="tug-mid" style={{ left: widthOf(left) - 1, right: widthOf(right) - 1 }}>
        <span className="tug-edge tug-edge-l" />
        <span className="tug-edge tug-edge-r" />
        <Knot x={Math.max(0, Math.min(1, at))} />
        <div className="tug-track">{children}</div>
      </div>
      <Champ kind={left} side="left" mood={moodOf(1 - a)} rope={ropeAt("l")} />
      <Champ kind={right} side="right" mood={moodOf(a)} rope={ropeAt("r")} />
    </div>
  );
}

// Fixed scatter for the particles (a seeded LCG), so a re-render never reshuffles them mid-flight.
function scatter(n: number, seed: number): number[][] {
  let s = seed;
  const r = () => ((s = (s * 16807) % 2147483647) - 1) / 2147483646;
  return Array.from({ length: n }, () => [r(), r(), r(), r(), r()]);
}
const EMBERS = scatter(18, 7);
const CONFETTI = scatter(40, 11);
const RGB: Record<Kind | "gold", string> = { bull: "34, 197, 94", bear: "239, 68, 68", gold: "251, 191, 36" };

function Trophy() {
  return (
    <svg className="tug-trophy" viewBox="0 0 24 26" aria-hidden>
      <defs>
        <linearGradient id="tug-gold" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#8a6418" /><stop offset=".45" stopColor="#ffe7a0" /><stop offset=".6" stopColor="#f5c542" /><stop offset="1" stopColor="#7a5512" />
        </linearGradient>
      </defs>
      <path d="M6 2 H18 V8 C18 12.5 15.4 15.5 12 15.5 C8.6 15.5 6 12.5 6 8 Z" fill="url(#tug-gold)" />
      <path d="M6 4 H2.5 C2.5 8.5 4 10.5 7 11 M18 4 H21.5 C21.5 8.5 20 10.5 17 11" fill="none" stroke="url(#tug-gold)" strokeWidth="1.6" />
      <rect x="10.6" y="15.2" width="2.8" height="4.6" fill="url(#tug-gold)" />
      <rect x="7" y="19.6" width="10" height="2.6" rx=".6" fill="url(#tug-gold)" />
      <rect x="5.5" y="22.2" width="13" height="2.8" rx=".8" fill="#5a3f10" />
    </svg>
  );
}

/**
 * How it ended, in place of the bar for the few seconds the close is shown: the winner turns to the
 * camera and celebrates (a bull rears, a bear roars), the loser slumps, the title flips in.
 */
export function TugParty({ win, kind, text }: { win: Kind; kind: "target" | "stop" | "close"; text: string }) {
  const lose: Kind = win === "bull" ? "bear" : "bull";
  const glow = kind === "target" ? RGB.gold : RGB[win];
  const winH = win === "bear" ? 54 : 60;   // the bear stands up to roar: room above its head
  return (
    <div className={`tug-party tug-party-${kind} tug-won-${win}`} style={{ "--glow-c": glow } as CSSProperties}>
      <div className="tug-rays" />
      {EMBERS.map(([x, d, delay, drift], i) => (
        <span
          key={i}
          className="tug-ember"
          style={{ left: `${4 + x * 40}%`, "--d": `${2.2 + d * 2}s`, "--delay": `${delay * 2.4}s`, "--x": `${(drift - 0.5) * 40}px` } as CSSProperties}
        />
      ))}
      <div className={`tug-party-win tug-${win} tug-pose-cheer`} style={{ height: winH, width: widthOf(win, winH) }}>
        <div><Animal kind={win} pose="cheer" /></div>
      </div>
      <div className="tug-party-title">
        <div className="tug-title-row">
          {kind === "target" && <Trophy />}
          <span className="tug-title-main">{win === "bull" ? "Bulls" : "Bears"} win</span>
          {kind === "target" && <Trophy />}
        </div>
        <div className="tug-title-sub">{text.replace(/^🎯\s*/u, "")}</div>
      </div>
      <div className={`tug-party-lose tug-${lose} tug-pose-slump`} style={{ width: widthOf(lose, 44) }}>
        <Animal kind={lose} pose="slump" />
      </div>
    </div>
  );
}

/** Gold falling through the whole ticket when a target is hit (its parent must be `relative`). */
export function TugConfetti({ win }: { win: Kind }) {
  const colors = ["#ffd36b", "#fff4cf", "#e0a526", "#ffffff", `rgb(${RGB[win]})`];
  return (
    <div className="tug-confetti" aria-hidden>
      {CONFETTI.map(([x, d, delay, spin, c], i) => (
        <i
          key={i}
          style={{
            left: `${x * 100}%`,
            "--c": colors[Math.floor(c * colors.length)],
            "--d": `${2.4 + d * 1.8}s`,
            "--delay": `${delay * 1.4}s`,
            "--x": `${(spin - 0.5) * 120}px`,
            "--rx": `${360 + spin * 720}deg`,
            "--ry": `${180 + d * 540}deg`,
            "--rz": `${(c - 0.5) * 360}deg`,
          } as CSSProperties}
        />
      ))}
    </div>
  );
}