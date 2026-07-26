import React from 'react'
import './SplashScreen.css'

export type SplashStepState = 'pending' | 'active' | 'done'

export interface SplashStep {
  label: string
  state: SplashStepState
}

/**
 * 启动遮罩：后台未连上前全屏覆盖，展示 Logo + 产品名 + 后台连接进度。
 * 连接成功后由父组件淡出卸载（不再白屏）。error 时展示错误与重试入口。
 */
export function SplashScreen({
  productName,
  version,
  steps,
  error,
  onRetry,
}: {
  productName: string
  version: string
  steps?: SplashStep[]
  error?: string | null
  onRetry?: () => void
}): React.ReactElement {
  return (
    <div className="wa-splash" role="status" aria-live="polite">
      <div className="wa-splash__glow" aria-hidden />
      <div className="wa-splash__card">
        <div className="wa-splash__logo" aria-hidden>
          <svg viewBox="0 0 48 48" width="56" height="56" role="img" aria-label="Work Agent 标志">
            <defs>
              <linearGradient id="waSplashLogo" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#5b8cff" />
                <stop offset="1" stopColor="#9a6bff" />
              </linearGradient>
            </defs>
            <rect x="2" y="2" width="44" height="44" rx="13" fill="url(#waSplashLogo)" />
            {/* 代码括号 + 斜杠，呼应「编码智能体」 */}
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
        </div>

        <div className="wa-splash__brand">
          <div className="wa-splash__name">{productName}</div>
          <div className="wa-splash__tagline">通用编码智能体</div>
        </div>
        <div className="wa-splash__version">v{version}</div>

        {error ? (
          <div className="wa-splash__error">
            <div className="wa-splash__error-title">无法连接到后台</div>
            <div className="wa-splash__error-msg">{error}</div>
            {onRetry && (
              <button type="button" className="wa-splash__retry" onClick={onRetry}>
                重试
              </button>
            )}
          </div>
        ) : (
          <>
            <ul className="wa-splash__steps">
              {steps?.map((s) => (
                <li key={s.label} className={`wa-splash__step is-${s.state}`}>
                  <span className="wa-splash__dot" aria-hidden />
                  <span className="wa-splash__label">{s.label}</span>
                </li>
              ))}
            </ul>
            <div className="wa-splash__bar">
              <div className="wa-splash__bar-fill" />
            </div>
            <div className="wa-splash__tip">正在连接后台服务，请稍候…</div>
          </>
        )}
      </div>
    </div>
  )
}
