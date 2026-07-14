## 本次任务：七选五

请讲解第 {{QUESTION_NUMBERS}} 题。

### 思路（按这个顺序想，讲的时候也按这个顺序讲）

1. **先判断空格的功能**：这个空是主题句、过渡句、举例句、解释句、总结句，还是号召句？
   位置本身就是线索——段首多半是主题句，段末多半是总结或过渡。
2. **再看空前空后**：这是七选五的命脉。
   - **代词指代**：选项里的 it / they / this / such 必须在空前找得到落点。
   - **关键词复现 / 同义复现**：空后出现的词，往往在正确选项里换了个说法。
   - **逻辑关系**：but / however / also / for example / as a result 这些词把空前空后拴死了。
   - **数字、并列结构、时间顺序**也算线索。
3. **最后比选项**：只分析 1–2 个最有竞争力的干扰项。
   七选五的干扰项通常错在：指代找不到落点、逻辑方向反了、或者话题对但层级不对
   （拿一个细节句去填主题句的位置）。

### 输出格式

{
  "questions": [
    {
      "number": "36",
      "answer": "D",
      "function": "过渡句",
      "clues": "空后出现 the same problem，选项 D 里的 this difficulty 正好接上；空前是转折 But。",
      "reasoning": "为什么 D 填进去读得通。",
      "distractors": [
        {"option": "F", "why_wrong": "话题对，但它是举例句，填在段首会让下一句的 for instance 没了着落。"}
      ]
    }
  ]
}

要求：
- `questions` 必须覆盖第 {{QUESTION_NUMBERS}} 题。
- `function` 从「主题句 / 过渡句 / 举例句 / 解释句 / 总结句 / 号召句」中选。
- `clues` 必须写出**具体的词**，不要只说「根据上下文」。
- 每题 `reasoning` 100 字以内。

### 题目原文（含 A–G 七个选项）

{{QUESTION_TEXT}}

### 答案

{{ANSWER_KEY}}

### 原卷官方解析

{{OFFICIAL_EXPLANATION}}
