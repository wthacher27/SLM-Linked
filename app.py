"""The model in ckpt/ as a web page and an HTTP API. `python app.py` serves it on localhost:7860;
`python slm.py deploy user/space` puts it on a Hugging Face Space, which runs this same file."""
import gradio as gr
import slm

model = slm.load('ckpt')


def write(prompt, best_of=slm.BEST_OF):
    return slm.generate(model, prompt[:300], best_of=int(best_of))  # training prompts stop at 300 chars too


demo = gr.Interface(
    write, gr.Textbox(label='Prompt', placeholder='Write a LinkedIn post about ...'),
    gr.Textbox(label='Post', buttons=['copy']),
    additional_inputs=gr.Slider(1, 32, slm.BEST_OF, step=1, label='Best of',
                                info='samples to pick from: more stays on topic more often, but takes longer'),
    examples=[[p] for p, _ in slm.EVAL[:6]], title='SLM-Linked',
    description='LinkedIn posts from a ~14M-parameter model trained from scratch. Each run samples fresh.',
    flagging_mode='never', api_name='write')

if __name__ == '__main__':
    demo.launch()
