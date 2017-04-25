"""
Simple server to serve a user facing interface allowing them to submit data for processing
and to see reports once the they are ready.

Purposes of this server:
1) Provide an interface for users to upload their data files
2) Provide an interface for users to view the results of the analysis
3) Generate necessary assets needed for 1) and 2), such as, job_id and plot images.
4) Generate all the tasks needed to complete a job
5) Future: Re-run tasks that failed

Architecture:
Frontend Flask server --> Amazon SNS --> Amazon Lambda
    |           |                           |   ^
    v           v                           |   |
Amazon         Amazon    <------------------+   |
S3             DynamoDB                         |
    |                                           |
    +-------------------------------------------+

Author: Angad Gill
"""

from flask import Flask, request, make_response, render_template, redirect, url_for, flash
from werkzeug.utils import secure_filename

import boto3
from boto3.dynamodb.conditions import Attr

import io
import os
import json

from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import seaborn as sns

import pandas as pd

DYNAMO_URL = 'https://dynamodb.us-west-1.amazonaws.com'
DYNAMO_TABLE = 'test_table'
DYNAMO_REGION = 'us-west-1'
S3_BUCKET = 'kmeansservice'
SNS_TOPIC_ARN = 'arn:aws:sns:us-west-1:000169391513:kmeans-service'

UPLOAD_FOLDER = 'data'
ALLOWED_EXTENSIONS = set(['csv'])


app = Flask(__name__)
app.secret_key = 'some_secret'


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/status/', methods=['GET', 'POST'])
@app.route('/status/<job_id>')
def status(job_id=None):
    """ Pull information on all tasks for a job from DynamoDB and render as a table """

    if request.method == 'POST':
        job_id = request.form['job_id']
        if job_id:
            return redirect(url_for('status', job_id=job_id))
        else:
            flash("Invalid job ID!", 'danger')
            return render_template('index.html')

    if job_id is None:
        job_id = request.args.get('job_id', None)

    if job_id:
        tasks = get_tasks(job_id)
        n_tasks = len(tasks)
        n_tasks_done = len([x for x in tasks if x['task_status']=='done'])
        per_done = '{:.1f}'.format(n_tasks_done/n_tasks*100)
        return render_template('status.html', job_id=job_id, n_tasks=n_tasks, n_tasks_done=n_tasks_done,
                               per_done=per_done, tasks=tasks)
    else:
        return render_template('index.html', error='Invalid Job ID.')


def get_tasks(job_id):
    """ Get a list of all task entries in DynamoDB for the given job_id. """
    dynamodb = boto3.resource('dynamodb', region_name=DYNAMO_REGION, endpoint_url=DYNAMO_URL)
    table = dynamodb.Table(DYNAMO_TABLE)
    response = table.scan(FilterExpression=Attr('job_id').eq(int(job_id)))
    tasks = response['Items']
    return tasks


@app.route('/report/<job_id>')
def report(job_id=None):
    return render_template('report.html', job_id=job_id)


@app.route('/plot/<job_id>')
def plot(job_id=None):
    sns.set(context='talk')

    df = pd.DataFrame(get_tasks(job_id))
    df = df.loc[:, ['k', 'covar_type', 'covar_tied', 'bic', 'aic']]
    df['covar_type'] = [x.capitalize() for x in df['covar_type']]
    df['covar_tied'] = [['Untied', 'Tied'][x] for x in df['covar_tied']]
    df['aic'] = df['aic'].astype('float')
    df['bic'] = df['bic'].astype('float')

    df = pd.melt(df, id_vars=['k', 'covar_type', 'covar_tied'], value_vars=['aic', 'bic'], var_name='metric')
    f = sns.factorplot(x='k', y='value', col='covar_type', row='covar_tied', hue='metric', data=df,
                       row_order=['Tied', 'Untied'], col_order=['Full', 'Diag', 'Spher'], legend=True, legend_out=True)
    f.set_titles("{col_name} {row_name}")

    fig = f.fig
    return fig_to_png(fig)


def fig_to_png(fig):
    canvas = FigureCanvas(fig)
    output = io.BytesIO()
    canvas.print_png(output)
    response = make_response(output.getvalue())
    response.mimetype = 'image/png'
    return response


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def upload_to_s3(filepath, filename, job_id):
    s3_file_key = '{}/{}/{}'.format(UPLOAD_FOLDER, job_id, filename)
    s3 = boto3.resource('s3')
    s3.meta.client.upload_file(filepath, S3_BUCKET, s3_file_key)
    return s3_file_key


@app.route('/submit', methods=['GET', 'POST'])
def submit():
    if request.method == 'POST':
        # Ensure that file is part of the post
        if 'file' not in request.files:
            flash("No file part in form submission!", 'danger')
            return redirect(url_for('index'))

        # Ensure that files were selected by user
        file = request.files['file']
        if file.filename == '':
            flash("No selected file!", 'danger')
            return redirect(url_for('index'))

        # Ensure that file type is allowed
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            job_id = int(request.form.get('job_id'))
            s3_file_key = upload_to_s3(filepath, filename, job_id)
            flash('File "{}" uploaded successfully!'.format(filename), 'success')
            n_tasks = submit_job(request.form, filepath, s3_file_key, job_id)

            flash('Your job ID is: {}.'.format(job_id), 'info')
            flash('{} tasks submitted!'.format(n_tasks), 'success')
            return redirect(url_for('status', job_id=job_id))

        else:
            filename = secure_filename(file.filename)
            flash('Incorrect file extension for file "{}"!'.format(filename), 'danger')
            return redirect(url_for('index'))

    else:
        return redirect(request.url)


def send_to_dynamo(id, job_id, task_id, covar_tied, covar_type, k, n_init, s3_file_key, columns, sns_message,
                   sns_subject, task_status):
    dynamodb = boto3.resource('dynamodb', region_name=DYNAMO_REGION, endpoint_url=DYNAMO_URL)
    table = dynamodb.Table(DYNAMO_TABLE)
    response = table.put_item(
        Item={'id': id, 'job_id': job_id, 'task_id': task_id, 'sns_message': sns_message, 'sns_subject': sns_subject,
              'covar_type': covar_type, 'covar_tied': covar_tied, 'k': k, 'n_init': n_init, 's3_file_key': s3_file_key,
              'columns': columns, 'task_status': task_status})
    return response


def send_to_sns(message, subject):
    sns = boto3.client('sns')
    response = sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject=subject)
    return response


def submit_job(form, filepath, s3_file_key, job_id):
    n_init = int(form.get('n_init'))
    n_experiments = int(form.get('n_experiments'))
    max_k = int(form.get('max_k'))
    covars = form.getlist('covars')
    df = pd.read_csv(filepath, nrows=1)
    exclude_columns = ['longitude', 'latitude']
    columns = [c for c in df.columns if c.lower() not in exclude_columns]
    task_status = 'pending'
    task_id = 0
    for _ in range(n_experiments):
        for k in range(1, max_k+1):
            for covar in covars:
                covar_type, covar_tied = covar.lower().split('-')
                covar_tied = covar_tied=='tied'
                id = int('{}'.format(job_id)+'{0:04d}'.format(task_id))
                payload = dict(id=id, k=k, covar_type=covar_type, covar_tied=covar_tied, n_init=n_init,
                               s3_file_key=s3_file_key, columns=columns)
                sns_message = json.dumps(payload)
                sns_subject = 'web test'

                send_to_dynamo(id, job_id, task_id, covar_tied, covar_type, k, n_init, s3_file_key, columns,
                               sns_message, sns_subject, task_status)
                send_to_sns(sns_message, sns_subject)
                task_id += 1
                # print('Submited: job_id:{}, task_id:{}'.format(job_id, task_id))
    return task_id


if __name__ == "__main__":
    app.run(debug=True)
