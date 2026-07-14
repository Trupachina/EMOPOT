from google import genai

client = genai.Client(api_key="AIzaSyAC3ls1OdOMVvZGhMUVKgqfdoNc1YpDc9o")

response = client.models.generate_content(
    model="gemini-2.5-flash", contents="Напиши рабочий код калькулятора c визуальной оболойчкой на Python! Визуал современный, опирайся на дизайны Google, Apple и Яндекс, у окна должна быть возможность ммасштабирования без ошибок! Добавь возможность ввода текста с клавиатуры! Без приветственных фраз и описания, только чистый код с коментариями!"
)
print(response.text)