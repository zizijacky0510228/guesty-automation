# Approved Guest Reply Answers

这些是业主人工确认过的回复口径。Webhook AI 回复时优先使用这里的答案；如果没有匹配口径，仍按限制条件升级邮件，不编造。

## 使用规则

- `Scope` 可以是 `All`，也可以是具体 property nickname token：`3505`、`383`、`2171`、`6550`。
- `Guest asks` 写客人问题类型，不需要逐字一样。
- `Approved reply style` 写以后类似问题要采用的回复方式。
- 如果答案只适用于某个物业，必须写清楚 Scope，不能套用到其他物业。
- 涉及日期变更、退款、提前退房、赔偿、付款纠纷、安全/法律/医疗问题，除非这里有非常明确的业主批准口径，否则仍然升级邮件。
- 不管客人使用什么语言发消息，都统一用英文回复。
- 入住信息、地址、门码、楼层、房间号、Wi-Fi、停车、check-in instructions 等问题，只能根据 Guesty 对话里已经发送过的自动入住信息或 host 历史消息回答；没有可靠内容就升级给业主，不能猜。
- 不要承诺额外提供、送达或安排毛巾、床品、床单、被套、枕头、毯子、纸巾或其他额外用品；我们只提供客人本次入住时配置的一套/一批用品。
- 拒绝无法满足的诉求时，可以使用抱歉口吻，表达我们也感到抱歉，但不能扩大承诺。

## Approved Answers

### 1. Early Check-In / Arriving Before 3pm

Scope: All in-scope properties, unless current conversation or listing says otherwise.

Guest asks:

- Can I check in early?
- Can I check in around 12pm/1pm/2pm?
- Is early check-in possible?

Approved reply style:

> You will receive the check in info and codes around 2:00pm on the check in date for self check in and the check in time starts from 3:00pm. You could check by 2:00pm if the room is ready for any early check in. Thanks

Notes:

- Do not promise early check-in before the room is ready.
- If guest asks for date change, reservation modification, or guaranteed early access, escalate to owner.

### 2. Luggage Storage / Leaving Bags Before Check-In Or After Check-Out

Scope: All in-scope properties, unless owner later approves a specific property storage option.

Guest asks:

- Can we leave luggage before check-in?
- Can we leave bags after check-out?
- Can we drop off luggage at the property/front desk/common area?

Approved reply style:

> Hi unfortunately we don’t have a secured place for baggage storage there. We don’t recommend leaving your luggage in the common area, as we cannot guarantee its safety. Thanks for your patience.

Notes:

- Do not suggest leaving luggage unattended.
- If guest asks for a nearby paid locker recommendation and no prior property-specific answer is available, escalate to owner.

### 3. Parking / Driveway / Street Parking

Scope: Use only when the current property history or conversation supports the same parking setup. For `6550`, prior host answer says free street parking is in front of the house. For other properties, if no clear property-specific parking answer is present, escalate to owner instead of guessing.

Guest asks:

- Is there parking?
- Is there a driveway or street parking only?
- Is a parking spot included?
- Can I park inside the property?

Approved reply style when street parking is supported:

> There are free street parkings available near by our property. If the front door street parking is unavailable, you could try to find one near by our area. Thanks!

Approved reply style for 6550 when guest asks parking:

> Hi there is free street parking in front of the house. Thanks

Approved reply style when parking is not included / not promised:

> Hi double checked for you, unfortunately only the listings with parking included there have the promise of access there. You may need to find a street parking space there or search for a parking lot around there. Thanks for your patience.

Notes:

- Do not promise driveway/private parking unless the check-in instructions or listing clearly include a parking spot.
- Do not tell guests to park inside the property or block the driveway unless owner specifically approved it.

### 4. Laundry / Washer And Dryer

Scope: Use only when the current property history or listing supports washer/dryer access.

Guest asks:

- Are there laundry facilities?
- Can we use the washer/dryer?
- Are there laundromats nearby?

Approved reply style:

> Hi, thank you for the question! We have washer and dryer available there for your convenience. You may feel free to use them during your stay.

Notes:

- If property-specific support is not clear from history or listing, escalate to owner.

### 5. Private Bedroom / Private Bathroom / Shared Spaces

Scope: Use only when current property/listing history supports private bedroom and private bathroom.

Guest asks:

- Is the bathroom private?
- Is the bathroom shared?
- Which spaces are shared?

Approved reply style:

> You will have your private bedroom with your private bathroom. The kitchen, laundry and living room are shared spaces with others. Thanks!

Notes:

- If guest asks about fees or cleaning fee and the exact fee is not available in context, do not invent it; answer only the bathroom/shared-space part and escalate fee question if needed.

### 6. Same Room / Room Change Between Consecutive Reservations

Scope: Only when Guesty conversation/reservation history clearly shows whether the guest booked the same room or a different room.

Guest asks:

- Can I stay in the same room?
- Do I need to move rooms for the next booking?
- I booked another stay; can I keep the same room?

Approved reply style if same room:

> Hi you have booked the same room there so you don’t need to move. Thanks

Approved reply style if same room and cleaning question is useful:

> Hi you have booked the same room there. Do you need the room cleaning there today? Thanks

Approved reply style if different room:

> You have booked a different room there. You could have all the personal items packed ahead and wait in your current room before the new room is ready for any early check in there. Thanks for your patience.

Notes:

- If the room/reservation match is not clear, escalate to owner.

### 7. Availability / Extra Room / Fully Booked

Scope: Only when platform or Guesty history clearly supports the answer.

Guest asks:

- Are these dates available?
- Any other rooms?
- Are you fully booked?

Approved reply style for general availability:

> Hi, please kindly check the availability directly through the booking platform there. Thanks

Approved reply style if already double-checked and fully booked:

> Hi double checked we are fully booked on that day. Thanks

Notes:

- Do not confirm real availability unless it is clearly supported.
- Requests to extend/add nights or payment links should still escalate unless owner has approved the exact rate and flow.

### 8. Hair Dryer

Scope: Use only when current property history supports the same location.

Guest asks:

- Is there a hairdryer?
- Where is the hairdryer?

Approved reply style:

> Hi we do have a hairdryer available there for your convenience, which should be under the sink in your bathroom. Thanks

### 9. Toilet Paper / Bathroom Supplies

Scope: Use only when current property history supports the same location.

Guest asks:

- Where is the toilet paper?
- Where can we find the provided bathroom supplies?

Approved reply style:

> There should have more under the sink in the bathroom.

Notes:

- Do not promise delivery or restocking of toilet paper, paper towels, toiletries, or other supplies.
- If the guest asks for extra supplies during the stay, use the extra-supplies policy below or escalate if it sounds like a missing/cleaning issue.

### 10. Minor Maintenance / Non-Urgent Issue

Scope: All in-scope properties for non-emergency, non-safety maintenance notes.

Guest says:

- Something is not working.
- Outlet is not working.
- They report a non-urgent issue.

Approved reply style:

> Hi, thank you for reaching out and for letting us know about the situation. Will forward to our team there shortly. Thank you for your patience!

Notes:

- Safety, injury, medical, legal, severe damage, flooding, fire, police, lockout, or urgent access problems must escalate to owner.

### 11. Noise / Other Guests / House Rule Concern

Scope: All in-scope properties for non-emergency noise complaints.

Guest says:

- Other guests are loud.
- Guests are using kitchen late.
- Someone is violating quiet hours.

Approved reply style when more info is needed:

> Any idea which room is that? Thanks

Approved reply style when guest provides details:

> Thanks for your information there. Forwarded to our team.

Notes:

- If the guest reports safety threat, police, violence, harassment, illegal activity, or urgent disturbance, escalate to owner.

### 12. Hot Water Delay

Scope: Use only when current property history supports this explanation.

Guest says:

- Hot water is slow/cold.
- Shower water did not get hot right away.

Approved reply style:

> Hi, it’s possible that many guests were using hot water at the same time during that period, so you might need to wait a bit longer for the hot water to come through. Thanks for your patience.

Notes:

- If guest reports no hot water persists, flooding, electrical issue, or serious complaint, escalate to owner.

### 13. Extra Towels / Linens / Bedding / Supplies During Stay

Scope: All in-scope properties.

Guest asks:

- Can we have more towels?
- Can you bring/provide extra towels?
- Can we get new/fresh towels, sheets, linens, bedding, blankets, pillows, or other extra supplies?

Approved reply style:

> Hi, we’re sorry for the inconvenience, but for each stay we provide one set of towels and bedding/linens for the guest. We’re unable to provide extra towels, linens, bedding, pillows, blankets or other extra supplies during the stay. Thanks for your understanding.

Notes:

- Do not say we will bring, provide, deliver, arrange, prepare, or restock extra items.
- Use a polite apologetic tone when declining the request, but keep the refusal clear.
- If washer/dryer access is clearly supported, you may add: `If needed, you may use the washer and dryer during your stay.`
- If the guest reports missing items at check-in, no towels/linens were provided at all, dirty bedding, or a cleaning issue, escalate to owner instead of denying.

### 14. AC / Heating Control

Scope: Use only when current property history supports the same setup.

Guest asks:

- Where is AC/heating control?
- How can I make the bedroom warmer?

Approved reply style:

> Hi the AC control panel is in the hallway there. Thanks

Follow-up style if they ask how to adjust:

> You could modify the temperature there. Thanks
