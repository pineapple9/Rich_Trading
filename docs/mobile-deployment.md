# ทดลอง Rich Trading บนมือถือผ่าน Streamlit Community Cloud

ใช้ entrypoint **`cloud/mobile_demo.py`** สำหรับเว็บทดลอง โดยคง `app.py` ไว้สำหรับแอปส่วนตัวในเครื่อง

## โค้ดบน GitHub และเว็บไซต์เป็นคนละขั้นตอน

ใช้สาขา `mobile-demo` สำหรับโค้ดเว็บทดลอง หลังนำโค้ดขึ้น GitHub แล้ว ต้องสร้างแอปบน Streamlit Community Cloud ตามขั้นตอนด้านล่างจึงจะได้ URL ที่เปิดจากมือถือได้ การมีไฟล์ใน GitHub หรือการ Publish สภาพแวดล้อม Codex ยังไม่ยืนยันว่าเว็บไซต์ Deploy สำเร็จ

## เว็บทดลองแตกต่างจากแอปส่วนตัวอย่างไร

- แต่ละเซสชันเบราว์เซอร์มีฐานข้อมูลพอร์ตจำลองและการตั้งค่าของตัวเอง ไม่ใช้ฐานข้อมูลร่วมกัน
- ไม่เปิดสมุดบันทึกเงินจริง และไม่อ่านฐานข้อมูลจาก `RICH_TRADING_DB`
- เริ่มจากหุ้นสมมติ/CSV ไม่มีข้อมูลราคาสด ไม่มีล็อกอิน BLS และไม่ส่งคำสั่งซื้อขาย
- ข้อมูลทดลองเป็นข้อมูลชั่วคราว ไม่รับประกันการเก็บเมื่อปิดเซสชัน เปลี่ยนเบราว์เซอร์ แอปหลับ หรือรีสตาร์ตเซิร์ฟเวอร์ ดาวน์โหลด CSV เพื่อเก็บผล
- ไม่มีบัญชีผู้ใช้ของแอปหรือการซิงก์พอร์ตระหว่างอุปกรณ์ หากต้องการใช้สมุดบันทึกเงินจริงผ่านเว็บ ต้องเตรียมการยืนยันตัวตนและฐานข้อมูลถาวรแยกต่างหาก

## ทดลอง entrypoint ก่อน Deploy

จาก root ของ repository:

```bash
uv sync --frozen --extra dev
uv run --frozen streamlit run cloud/mobile_demo.py
```

`cloud/.streamlit/config.toml` กำหนดให้เฉพาะ entrypoint นี้รับการเชื่อมต่อเว็บที่ `0.0.0.0` ส่วน config ของ `app.py` ยังคงใช้ loopback ตามเดิม เวอร์ชัน Streamlit ที่รองรับ config แยกตาม script ถูกล็อกไว้ใน `uv.lock`

## ขั้นตอนสร้างเว็บไซต์

1. นำโค้ดแอปและไฟล์ dependencies ขึ้น GitHub **เมื่อเจ้าของอนุญาต** อย่านำ `var/`, `.venv/`, `.streamlit/secrets.toml` หรือฐานข้อมูลส่วนตัวขึ้นไปด้วย
2. สมัคร/เข้าสู่ระบบ [Streamlit Community Cloud](https://share.streamlit.io/) ด้วยบัญชีของคุณ และให้สิทธิ์เข้าถึง repository ที่ต้องการผ่านหน้าบริการโดยตรง ไม่ต้องส่ง token หรือรหัสผ่านในแชต
3. เลือก **Create app → Yup, I have an app** แล้วกรอก:

   | ช่อง | ค่า |
   |---|---|
   | Repository | `pineapple9/Rich_Trading` |
   | Branch | สาขาที่มีโค้ดแอปซึ่ง push แล้ว |
   | Main file path | `cloud/mobile_demo.py` |
   | Advanced settings → Python version | `3.12` |

4. โครงการนี้มี `uv.lock` ที่ root; Community Cloud รองรับการติดตั้งจากไฟล์นี้ จึงไม่ต้องเพิ่ม `requirements.txt` ซ้ำ เว็บทดลองไม่ต้องใช้ secrets
5. กด **Deploy** และตรวจ build/runtime logs จนแอปเริ่มทำงาน หากผิดพลาดให้ตรวจ branch, main file path, Python version และ dependency logs
6. เปิด **URL จริงที่บริการสร้างให้** จาก Safari/Chrome บนมือถือ เลือกหน้าแผนเทรด พอร์ตจำลอง และทดสอบย้อนหลังผ่านเมนูด้านซ้าย
7. ตรวจ Settings → Sharing ว่าสิทธิ์เข้าชมตรงกับที่ต้องการ การจำกัดสิทธิ์ผ่านบริการไม่ทำให้พอร์ตชั่วคราวของแต่ละเซสชันกลายเป็นพอร์ตที่ซิงก์ข้ามเครื่อง

ไม่ควรเลือก `app.py` เป็น entrypoint ของเว็บทดลองสาธารณะ เพราะไฟล์นั้นเปิดสมุดบันทึกส่วนตัวและฐานข้อมูลร่วมของแอปในเครื่อง

## การตรวจสอบที่ทำได้ก่อนมี URL จริง

```bash
uv run --frozen pytest -q
```

AppTest ตรวจการแยกข้อมูลระหว่างเซสชัน การไม่แตะฐานข้อมูลส่วนตัว และการซ่อนหน้าบันทึกเงินจริง ส่วนการเปิดเว็บบน Safari/Chrome จริงต้องตรวจอีกครั้งหลัง Deploy สำเร็จ

## เอกสารทางการ

- [Deploy your app](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Dependencies และการรองรับ uv.lock](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- [File organization และ config แยกตาม script](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization)
- [App settings และสิทธิ์เข้าถึง](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/app-settings)
