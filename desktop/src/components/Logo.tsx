import React from 'react'

/**
 * 应用 Logo：渐变圆角方块 + 白色代码括号字形（< > /），与开屏 SplashScreen 完全一致。
 * 无图片资源，纯 SVG，可随 size 缩放；多处复用以保证品牌统一。
 */
export function Logo({
  size = 22,
  className,
}: {
  size?: number
  className?: string
}): React.ReactElement {
  const rawId = React.useId()
  const gid = `waLogo-${rawId.replace(/[^a-zA-Z0-9_-]/g, '')}`
  return (
    <svg
      className={className}
      viewBox="0 0 48 48"
      width={size}
      height={size}
      role="img"
      aria-label="Work Agent 标志"
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#5b8cff" />
          <stop offset="1" stopColor="#9a6bff" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="44" height="44" rx="13" fill={`url(#${gid})`} />
      <path
        d="M17 18 L11 24 L17 30"
        fill="none"
        stroke="#ffffff"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M31 18 L37 24 L31 30"
        fill="none"
        stroke="#ffffff"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line
        x1="26"
        y1="15"
        x2="22"
        y2="33"
        stroke="#ffffff"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  )
}
