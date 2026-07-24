// 流式 Markdown 渲染（react-markdown + remark-gfm + rehype-highlight）。
// 增量场景：父级把累积后的整段 content 传入，由 React 重渲染（diff 由 react-markdown 内部处理，不闪烁）。

import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github.css'

export function Markdown({ text }: { text: string }): React.ReactElement {
  return (
    <div className="wa-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
